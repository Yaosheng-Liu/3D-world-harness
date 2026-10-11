"""Persistent scene and human feedback state for the local 3D review harness."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
import math
import os
import re
import struct
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
INLINE_REFERENCE_RE = re.compile(
    r"\[\[(?:(?P<simple_kind>object|annotation|image|scene):(?P<simple_id>[A-Za-z0-9_-]{1,64})"
    r"|node:(?P<node_object>[A-Za-z][A-Za-z0-9_-]{0,63}):"
    r"(?P<node_path>[0-9]{1,5}(?:/[0-9]{1,5}){0,31}))\]\]"
)
INLINE_REFERENCE_START_RE = re.compile(r"\[\[(?:object|annotation|node|image|scene):")
CLIENT_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
ASSET_RE = re.compile(r"^/assets/[0-9a-f]{32}\.glb$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_MODEL_BYTES = 100 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_REFERENCE_BYTES = 25 * 1024 * 1024
MAX_REFERENCES = 8
MAX_CROPS = 8
MAX_IMAGE_REFS = 8


class APIError(Exception):
    def __init__(self, status: int, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _vector(value: Any, name: str, *, positive: bool = False) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise APIError(400, f"{name} must be a three-number array")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise APIError(400, f"{name} must contain finite numbers")
        if abs(item) > 1_000_000 or (positive and item <= 0):
            raise APIError(400, f"{name} has an out-of-range value")
        result.append(float(item))
    return result


def _safe_json(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise APIError(400, "feedback is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise APIError(400, "numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > 32_000:
            raise APIError(400, "text field is too long")
        return
    if isinstance(value, list):
        if len(value) > 1000:
            raise APIError(400, "array is too long")
        for item in value:
            _safe_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100:
            raise APIError(400, "object has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 100:
                raise APIError(400, "invalid field name")
            _safe_json(item, depth=depth + 1)
        return
    raise APIError(400, "value is not valid JSON")


def _glb_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise APIError(400, f"GLB {label} must be an integer >= {minimum}")
    return value


def _glb_index(value: Any, count: int, label: str) -> int:
    index = _glb_int(value, label)
    if index >= count:
        raise APIError(400, f"GLB {label} index is out of bounds")
    return index


def _glb_array(document: dict[str, Any], key: str) -> list[Any]:
    value = document.get(key, [])
    if not isinstance(value, list):
        raise APIError(400, f"GLB {key} must be an array")
    return value


def _reject_glb_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _validate_glb_document(document: Any, handle: Any, bin_start: int | None, bin_length: int) -> None:
    """Check self-contained core glTF resources before the GLB is published."""
    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict) or document["asset"].get("version") != "2.0":
        raise APIError(400, "GLB asset version must be 2.0")

    # This publishing endpoint requires BIN/bufferView resources. Even data URIs
    # are rejected: they are self-contained but bypass the single checked BIN
    # resource path. Reject URI fields in extensions as well as core objects.
    pending: list[Any] = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "uri" in value:
                raise APIError(400, "GLB URI resources are not allowed, including data URIs; embed resources in BIN")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)

    buffers = _glb_array(document, "buffers")
    if len(buffers) > 1:
        raise APIError(400, "GLB may contain only one BIN-backed buffer")
    buffer_length = 0
    if buffers:
        if not isinstance(buffers[0], dict):
            raise APIError(400, "GLB buffer must be an object")
        buffer_length = _glb_int(buffers[0].get("byteLength"), "buffer.byteLength", minimum=1)
        if bin_start is None:
            raise APIError(400, "GLB BIN chunk is missing for buffer 0")
        if not buffer_length <= bin_length <= buffer_length + 3:
            raise APIError(400, "GLB BIN chunk length does not match buffer 0")
        if bin_length > buffer_length:
            handle.seek(bin_start + buffer_length)
            if handle.read(bin_length - buffer_length) != bytes(bin_length - buffer_length):
                raise APIError(400, "GLB BIN padding must be zero")
    elif bin_start is not None:
        raise APIError(400, "GLB BIN chunk has no buffer declaration")

    views = _glb_array(document, "bufferViews")
    for index, view in enumerate(views):
        if not isinstance(view, dict):
            raise APIError(400, f"GLB bufferView {index} must be an object")
        _glb_index(view.get("buffer"), len(buffers), f"bufferView {index}.buffer")
        offset = _glb_int(view.get("byteOffset", 0), f"bufferView {index}.byteOffset")
        length = _glb_int(view.get("byteLength"), f"bufferView {index}.byteLength", minimum=1)
        if offset + length > buffer_length:
            raise APIError(400, f"GLB bufferView {index} exceeds its buffer")
        if "byteStride" in view:
            stride = _glb_int(view["byteStride"], f"bufferView {index}.byteStride", minimum=4)
            if stride > 252 or stride % 4 or stride > length:
                raise APIError(400, f"GLB bufferView {index} has invalid byteStride")

    component_sizes = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    accessor_shapes = {"SCALAR": (1, 1), "VEC2": (1, 2), "VEC3": (1, 3), "VEC4": (1, 4), "MAT2": (2, 2), "MAT3": (3, 3), "MAT4": (4, 4)}
    accessors = _glb_array(document, "accessors")
    for index, accessor in enumerate(accessors):
        if not isinstance(accessor, dict):
            raise APIError(400, f"GLB accessor {index} must be an object")
        component_type = accessor.get("componentType")
        accessor_type = accessor.get("type")
        shape = accessor_shapes.get(accessor_type) if isinstance(accessor_type, str) else None
        if type(component_type) is not int or component_type not in component_sizes or shape is None:
            raise APIError(400, f"GLB accessor {index} has invalid type or componentType")
        count = _glb_int(accessor.get("count"), f"accessor {index}.count", minimum=1)
        component_size = component_sizes[component_type]
        columns, rows = shape
        column_size = rows * component_size
        element_size = columns * ((column_size + 3) // 4 * 4) if columns > 1 else column_size
        if "bufferView" in accessor:
            view_index = _glb_index(accessor["bufferView"], len(views), f"accessor {index}.bufferView")
            view = views[view_index]
            offset = _glb_int(accessor.get("byteOffset", 0), f"accessor {index}.byteOffset")
            stride = view.get("byteStride", element_size)
            if offset % component_size or view.get("byteOffset", 0) % component_size or stride < element_size:
                raise APIError(400, f"GLB accessor {index} has invalid alignment or stride")
            if offset + (count - 1) * stride + element_size > view["byteLength"]:
                raise APIError(400, f"GLB accessor {index} exceeds its bufferView")
        elif "byteOffset" in accessor:
            raise APIError(400, f"GLB accessor {index} has byteOffset without bufferView")

        sparse = accessor.get("sparse")
        if sparse is not None:
            if not isinstance(sparse, dict):
                raise APIError(400, f"GLB accessor {index}.sparse must be an object")
            sparse_count = _glb_int(sparse.get("count"), f"accessor {index}.sparse.count", minimum=1)
            if sparse_count > count:
                raise APIError(400, f"GLB accessor {index}.sparse.count exceeds count")
            for label, item_size, alignment in (("indices", None, None), ("values", element_size, component_size)):
                item = sparse.get(label)
                if not isinstance(item, dict):
                    raise APIError(400, f"GLB accessor {index}.sparse.{label} must be an object")
                view_index = _glb_index(item.get("bufferView"), len(views), f"accessor {index}.sparse.{label}.bufferView")
                view = views[view_index]
                if "byteStride" in view or "target" in view:
                    raise APIError(400, f"GLB sparse {label} bufferView cannot have stride or target")
                if label == "indices":
                    index_type = item.get("componentType")
                    if type(index_type) is not int or index_type not in {5121, 5123, 5125}:
                        raise APIError(400, f"GLB accessor {index}.sparse.indices has invalid componentType")
                    item_size = alignment = component_sizes[index_type]
                offset = _glb_int(item.get("byteOffset", 0), f"accessor {index}.sparse.{label}.byteOffset")
                if offset % alignment or view.get("byteOffset", 0) % alignment or offset + sparse_count * item_size > view["byteLength"]:
                    raise APIError(400, f"GLB accessor {index}.sparse.{label} exceeds its bufferView")

    images = _glb_array(document, "images")
    image_formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise APIError(400, f"GLB image {index} must be an object")
        view_index = _glb_index(image.get("bufferView"), len(views), f"image {index}.bufferView")
        mime = image.get("mimeType")
        if not isinstance(mime, str) or mime not in image_formats:
            raise APIError(400, f"GLB image {index} has unsupported mimeType")
        view = views[view_index]
        if "byteStride" in view or "target" in view:
            raise APIError(400, f"GLB image {index} bufferView cannot have stride or target")
        assert bin_start is not None
        handle.seek(bin_start + view.get("byteOffset", 0))
        data = handle.read(view["byteLength"])
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as decoded:
                width, height = decoded.size
                if decoded.format != image_formats[mime] or width <= 0 or height <= 0 or width * height > 50_000_000:
                    raise APIError(400, f"GLB image {index} bytes do not match mimeType")
                decoded.load()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise APIError(400, f"GLB image {index} bytes do not match mimeType") from exc


def _default_objects() -> list[dict[str, Any]]:
    """A new project starts with references and no invented geometry."""
    return []


class SceneStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.assets_dir = self.data_dir / "assets"
        self.screenshots_dir = self.data_dir / "screenshots"
        self.media_dir = self.data_dir / "media"
        self.state_path = self.data_dir / "state.json"
        self.token_path = self.data_dir / "control_token"
        self.browser_token_path = self.data_dir / "browser_token"
        self.lock = threading.RLock()
        for directory in (self.data_dir, self.assets_dir, self.screenshots_dir, self.media_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.control_token = self._control_token()
        self.browser_token = self._read_or_create_token(self.browser_token_path)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            # Existing v1 state and review sessions stay readable after upgrading.
            self.state["schema_version"] = 2
            for session in self.state.get("sessions", {}).values():
                session.setdefault("reference_images", [])
                session.setdefault("reference_clip", None)
                session.setdefault("feedback_count", 0)
        else:
            self.state = {"schema_version": 2, "scene": {"revision": 1, "objects": _default_objects()}, "sessions": {}, "feedback": []}
            self._save()

    def _read_or_create_token(self, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="ascii").strip()
        token = uuid.uuid4().hex + uuid.uuid4().hex
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(token + "\n")
        except FileExistsError:
            return path.read_text(encoding="ascii").strip()
        return token

    def _control_token(self) -> str:
        return self._read_or_create_token(self.token_path)

    def _save(self) -> None:
        fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def scene(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.state["scene"])

    def _validate_object(self, obj: Any) -> dict[str, Any]:
        if not isinstance(obj, dict):
            raise APIError(400, "each scene object must be an object")
        object_id = obj.get("id")
        if not isinstance(object_id, str) or not ID_RE.fullmatch(object_id):
            raise APIError(400, "object id must start with a letter and use letters, numbers, '_' or '-'")
        kind = obj.get("type")
        if kind not in {"box", "sphere", "cylinder", "model"}:
            raise APIError(400, "object type must be box, sphere, cylinder or model")
        name = obj.get("name", object_id)
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise APIError(400, "object name must contain 1 to 120 characters")
        position = _vector(obj.get("position"), "position")
        size = _vector(obj.get("size"), "size", positive=True)
        rotation = _vector(obj.get("rotation", [0, 0, 0]), "rotation")
        color = obj.get("color", "#aaaaaa")
        if not isinstance(color, str) or not COLOR_RE.fullmatch(color):
            raise APIError(400, "color must be a #rrggbb value")
        result = {"id": object_id, "name": name, "type": kind, "position": position, "size": size, "rotation": rotation, "color": color}
        if kind == "model":
            url = obj.get("url")
            if not isinstance(url, str) or not ASSET_RE.fullmatch(url):
                raise APIError(400, "model url must be an imported /assets/*.glb file")
            if not (self.assets_dir / url.rsplit("/", 1)[-1]).is_file():
                raise APIError(400, "model asset does not exist")
            result["url"] = url
        if "metadata" in obj:
            metadata = obj["metadata"]
            if not isinstance(metadata, dict):
                raise APIError(400, "metadata must be an object")
            _safe_json(metadata)
            if len(json.dumps(metadata, ensure_ascii=False)) > 16_000:
                raise APIError(400, "metadata is too large")
            result["metadata"] = metadata
        return result

    def _validate_objects(self, objects: Any) -> list[dict[str, Any]]:
        if not isinstance(objects, list) or len(objects) > 500:
            raise APIError(400, "objects must be an array with at most 500 items")
        validated = [self._validate_object(obj) for obj in objects]
        ids = [obj["id"] for obj in validated]
        if len(ids) != len(set(ids)):
            raise APIError(400, "scene object ids must be unique")
        return validated

    def replace_scene(self, expected_revision: int, objects: Any) -> dict[str, Any]:
        with self.lock:
            self._check_revision(expected_revision)
            validated = self._validate_objects(objects)
            self.state["scene"] = {"revision": expected_revision + 1, "objects": validated}
            self._save()
            if "workspace" in self.state:
                self.workspace_event("scene_published", {"scene_revision": expected_revision + 1})
            return self.scene()

    def _check_revision(self, revision: Any) -> None:
        if type(revision) is not int or revision < 1:
            raise APIError(400, "expected_revision must be a positive integer")
        if revision != self.state["scene"]["revision"]:
            raise APIError(409, f"scene revision changed to {self.state['scene']['revision']}; reload and retry")

    def update_scene(self, expected_revision: int, changes: Any) -> dict[str, Any]:
        with self.lock:
            self._check_revision(expected_revision)
            if not isinstance(changes, list) or not 1 <= len(changes) <= 500:
                raise APIError(400, "changes must be a nonempty array with at most 500 items")
            objects = copy.deepcopy(self.state["scene"]["objects"])
            for change in changes:
                if not isinstance(change, dict):
                    raise APIError(400, "each change must be an object")
                op = change.get("op", "update")
                if op == "add":
                    objects.append(change.get("object"))
                elif op in {"update", "delete"}:
                    object_id = change.get("object_id")
                    index = next((i for i, obj in enumerate(objects) if obj["id"] == object_id), None)
                    if index is None:
                        raise APIError(404, f"scene object {object_id!r} not found")
                    if op == "delete":
                        objects.pop(index)
                    else:
                        fields = change.get("fields")
                        if not isinstance(fields, dict) or not fields:
                            raise APIError(400, "update change needs nonempty fields")
                        if "id" in fields or "type" in fields or "url" in fields:
                            raise APIError(400, "object id, type and model url cannot be changed in place")
                        if set(fields) - {"name", "position", "size", "rotation", "color", "metadata"}:
                            raise APIError(400, "update contains unsupported object fields")
                        objects[index].update(fields)
                else:
                    raise APIError(400, "change op must be add, update or delete")
            return self.replace_scene(expected_revision, objects)

    @staticmethod
    def _image_kind(data: bytes) -> tuple[str, str]:
        from PIL import Image, UnidentifiedImageError

        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
            raise APIError(400, "image must be a valid PNG or JPEG")
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width > 32768 or height > 32768 or width * height > 50_000_000:
                    raise APIError(400, "image dimensions are too large")
                kind = image.format
                image.verify()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise APIError(400, "image must be a valid PNG or JPEG") from exc
        if kind == "PNG":
            return "png", "image/png"
        if kind == "JPEG":
            return "jpg", "image/jpeg"
        raise APIError(400, "image must be a valid PNG or JPEG")

    @classmethod
    def _decode_image_data_url(cls, data_url: Any, *, max_bytes: int = MAX_SCREENSHOT_BYTES) -> bytes:
        if not isinstance(data_url, str):
            raise APIError(400, "image must be a PNG or JPEG data URL")
        match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", data_url)
        if not match or len(data_url) > 4 * ((max_bytes + 2) // 3) + 64:
            raise APIError(400, "image must be a PNG or JPEG data URL within the size limit")
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except binascii.Error as exc:
            raise APIError(400, "invalid image encoding") from exc
        if not data or len(data) > max_bytes:
            raise APIError(400, "image exceeds the size limit")
        _, mime = cls._image_kind(data)
        if mime != f"image/{match.group(1)}":
            raise APIError(400, "image MIME type does not match its bytes")
        return data

    def _write_media(self, data: bytes) -> str:
        extension, _ = self._image_kind(data)
        name = f"{uuid.uuid4().hex}.{extension}"
        with (self.media_dir / name).open("xb") as handle:
            handle.write(data)
        return f"/media/{name}"

    def _read_reference_path(self, local_path: Any) -> tuple[str, bytes]:
        if not isinstance(local_path, str) or not local_path:
            raise APIError(400, "reference_images must contain local image file paths")
        try:
            source = Path(local_path).expanduser().resolve(strict=True)
            if not source.is_file() or not 0 < source.stat().st_size <= MAX_REFERENCE_BYTES:
                raise APIError(400, "reference image must be a file up to 25 MB")
            with source.open("rb") as handle:
                data = handle.read(MAX_REFERENCE_BYTES + 1)
        except (OSError, RuntimeError) as exc:
            raise APIError(400, "reference image cannot be read") from exc
        if len(data) > MAX_REFERENCE_BYTES:
            raise APIError(400, "reference image exceeds 25 MB")
        self._image_kind(data)
        return source.name[:180], data

    def create_session(self, reference_images: Any = None, *, reference_session_id: Any = None) -> dict[str, Any]:
        with self.lock:
            if reference_images is not None and reference_session_id is not None:
                raise APIError(400, "choose reference_images or reference_session_id")
            references: list[dict[str, str]] = []
            reference_clip = None
            if reference_session_id is not None:
                previous = self.state["sessions"].get(reference_session_id)
                if previous is None:
                    raise APIError(404, "reference session not found")
                references = copy.deepcopy(previous.get("reference_images", []))
                reference_clip = copy.deepcopy(previous.get("reference_clip"))
            elif reference_images is not None:
                if not isinstance(reference_images, list) or len(reference_images) > MAX_REFERENCES:
                    raise APIError(400, "reference_images must be an array of at most 8 local paths")
                prepared = [self._read_reference_path(path) for path in reference_images]
                references = [{"id": uuid.uuid4().hex, "url": self._write_media(data), "name": name} for name, data in prepared]
            session_id = uuid.uuid4().hex
            session = {"session_id": session_id, "scene_revision": self.state["scene"]["revision"], "status": "open", "created_at": _now(), "feedback_count": 0, "reference_images": references, "reference_clip": reference_clip}
            self.state["sessions"][session_id] = session
            self._save()
            return copy.deepcopy(session)

    def add_reference(self, session_id: str, name: Any, data_url: Any) -> dict[str, str]:
        with self.lock:
            session = self.state["sessions"].get(session_id)
            if session is None:
                raise APIError(404, "session not found")
            if session["status"] != "open":
                raise APIError(409, "session is closed")
            references = session.setdefault("reference_images", [])
            if len(references) >= MAX_REFERENCES:
                raise APIError(400, "session already has 8 reference images")
            if not isinstance(name, str) or not 1 <= len(name) <= 180 or name != Path(name).name or any(ord(char) < 32 for char in name):
                raise APIError(400, "name must be a short file name")
            data = self._decode_image_data_url(data_url, max_bytes=MAX_REFERENCE_BYTES)
            reference = {"id": uuid.uuid4().hex, "url": self._write_media(data), "name": name}
            references.append(reference)
            self._save()
            return copy.deepcopy(reference)

    def set_reference_clip(self, session_id: str, payload: Any, *, local_frames: list[dict[str, Any]] | None = None,
                           source_type: str = "sequence", byte_limit: int | None = None) -> dict[str, Any]:
        from dynamic import MAX_BROWSER_CLIP_BYTES, decode_video, fps_value, prepare_clip, sample_video

        # Check the destination before decoding; no session lock held during ffmpeg.
        session = self.get_session(session_id)
        if session["status"] != "open":
            raise APIError(409, "session is closed")
        if not isinstance(payload, dict):
            raise APIError(400, "clip must be an object")
        if payload.get("clear") is True:
            if set(payload) != {"clear"}:
                raise APIError(400, "clear cannot be combined with clip data")
            clip = None
        else:
            if "video_data_url" in payload:
                if "frames" in payload or local_frames is not None:
                    raise APIError(400, "choose clip frames or a video")
                video = decode_video(payload["video_data_url"])
                with tempfile.TemporaryDirectory(prefix="scene-feedback-upload-") as temporary:
                    path = Path(temporary) / "clip.video"
                    path.write_bytes(video)
                    local_frames, duration = sample_video(path, fps_value(payload.get("fps", 10)))
                payload = {**payload, "fps": fps_value(payload.get("fps", 10)), "duration_sec": duration}
                source_type = "video"
            clip = prepare_clip(self, payload, local_frames=local_frames, source_type=source_type,
                                byte_limit=byte_limit or MAX_BROWSER_CLIP_BYTES, write_media=False)
        return self._set_prepared_reference_views(session_id, payload, [] if clip is None else [clip])

    def _set_prepared_reference_views(self, session_id: str, payload: dict[str, Any], prepared: list[dict[str, Any]]) -> dict[str, Any]:
        """Publish all validated views together, preserving the old group on failure."""
        from dynamic import MAX_REFERENCE_VIEWS, clip_name, publish_prepared_clip, reference_views
        append = payload.get("append_view", False)
        if type(append) is not bool:
            raise APIError(400, "append_view must be a boolean")
        replace_id = payload.get("replace_view_id")
        if replace_id is not None and (not isinstance(replace_id, str) or not replace_id):
            raise APIError(400, "replace_view_id must identify a current view")
        if append and replace_id is not None:
            raise APIError(400, "choose append_view or replace_view_id")
        with self.lock:
            session = self.state["sessions"].get(session_id)
            if session is None or session["status"] != "open":
                raise APIError(409, "session changed while importing the clip")
            previous = session.get("reference_clip")
            old_views = reference_views(previous)
            if replace_id is not None and (len(prepared) != 1 or replace_id not in {view["clip_id"] for view in old_views}):
                raise APIError(400, "replace_view_id must identify one current reference view")
            count = len(old_views) + len(prepared) if append else len(old_views) if replace_id else len(prepared)
            if count > MAX_REFERENCE_VIEWS or (not prepared and payload.get("clear") is not True):
                raise APIError(400, "reference group must contain 1 to 8 views")
            group_name = payload.get("group_name")
            if group_name is not None:
                group_name = clip_name(group_name)
            published = [publish_prepared_clip(self, view) for view in prepared]
            if replace_id:
                published[0]["clip_id"] = replace_id
                views = [published[0] if view["clip_id"] == replace_id else view for view in old_views]
            else:
                views = old_views + published if append else published
            clip = copy.deepcopy(views[0]) if views else None
            if clip is not None:
                clip.pop("views", None)
                if len(views) > 1:
                    clip["views"] = copy.deepcopy(views[1:])
                clip["views_revision"] = (previous or {}).get("views_revision", 0) + 1
                if group_name is not None:
                    clip["group_name"] = group_name
            session["reference_clip"] = clip
            self._save()
            return {"session_id": session_id, "reference_clip": copy.deepcopy(clip)}

    @staticmethod
    def _normalize_reference_camera(camera: Any) -> dict[str, Any]:
        """Validate a calibrated Three.js camera in the GLB world coordinate frame.

        camera_to_world is a row-major 4x4 transform. Its local camera axes are
        +X right, +Y up and -Z forward; the GLB's up axis is deliberately not
        assumed here. Intrinsics use pixels of the stored reference image.
        """
        if not isinstance(camera, dict):
            raise APIError(400, "reference camera must be an object")
        matrix = camera.get("camera_to_world")
        if not isinstance(matrix, list) or len(matrix) != 4 or any(not isinstance(row, list) or len(row) != 4 for row in matrix):
            raise APIError(400, "camera_to_world must be a row-major 4x4 matrix")
        for row in matrix:
            for number in row:
                if type(number) not in (int, float) or not math.isfinite(number) or abs(number) > 1_000_000:
                    raise APIError(400, "camera_to_world must contain finite numbers")
        if any(abs(float(matrix[3][index]) - expected) > 1e-6 for index, expected in enumerate((0, 0, 0, 1))):
            raise APIError(400, "camera_to_world must have a homogeneous bottom row")
        rotation = [[float(matrix[row][column]) for column in range(3)] for row in range(3)]
        for row in range(3):
            for other in range(3):
                dot = sum(rotation[index][row] * rotation[index][other] for index in range(3))
                if abs(dot - (1.0 if row == other else 0.0)) > 0.003:
                    raise APIError(400, "camera_to_world rotation must be orthonormal")
        determinant = (rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
                       - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
                       + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0]))
        if abs(determinant - 1.0) > 0.003:
            raise APIError(400, "camera_to_world rotation must be right-handed")
        intrinsics = camera.get("intrinsics")
        if not isinstance(intrinsics, dict):
            raise APIError(400, "camera intrinsics must be an object")
        width, height = intrinsics.get("width"), intrinsics.get("height")
        if type(width) is not int or type(height) is not int or not (1 <= width <= 20000 and 1 <= height <= 20000):
            raise APIError(400, "camera width and height must be positive image dimensions")
        values = {}
        for field in ("fx", "fy", "cx", "cy"):
            value = intrinsics.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1_000_000:
                raise APIError(400, f"camera {field} must be a finite number")
            values[field] = float(value)
        if values["fx"] <= 0 or values["fy"] <= 0:
            raise APIError(400, "camera focal lengths must be positive")
        if not (0 <= values["cx"] <= width and 0 <= values["cy"] <= height):
            raise APIError(400, "camera principal point must lie inside the image")
        result = {
            "camera_to_world": [[float(value) for value in row] for row in matrix],
            "intrinsics": {"width": width, "height": height, **values},
        }
        if "distortion" in camera:
            distortion = camera["distortion"]
            if not isinstance(distortion, list) or len(distortion) != 5 or any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10 for value in distortion):
                raise APIError(400, "camera distortion must be five finite OpenCV coefficients")
            result["distortion"] = [float(value) for value in distortion]
        if "calibration_status" in camera:
            status = camera["calibration_status"]
            if not isinstance(status, str) or not 1 <= len(status) <= 200 or any(ord(char) < 32 for char in status):
                raise APIError(400, "calibration_status must be a short text label")
            result["calibration_status"] = status
        if "image_undistorted" in camera:
            if type(camera["image_undistorted"]) is not bool:
                raise APIError(400, "image_undistorted must be a boolean")
            result["image_undistorted"] = camera["image_undistorted"]
        return result

    @staticmethod
    def _image_dimensions(data: bytes) -> tuple[int, int]:
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as image:
                return image.size
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise APIError(400, "reference image cannot be decoded") from exc

    def set_reference_cameras(self, session_id: str, updates: Any) -> dict[str, Any]:
        """Attach calibrated views without changing reference pixels or scene revision."""
        from dynamic import MAX_CLIP_FRAMES, MAX_REFERENCE_VIEWS, reference_frames
        with self.lock:
            session = self.state["sessions"].get(session_id)
            if session is None:
                raise APIError(404, "session not found")
            if session["status"] != "open":
                raise APIError(409, "session is closed")
            if not isinstance(updates, list) or not 1 <= len(updates) <= MAX_REFERENCES + MAX_CLIP_FRAMES * MAX_REFERENCE_VIEWS:
                raise APIError(400, "cameras must contain bounded reference updates")
            dynamic_references = reference_frames(session.get("reference_clip"))
            references = {reference["id"]: reference for reference in session.get("reference_images", []) + dynamic_references}
            prepared = []
            seen = set()
            for item in updates:
                if not isinstance(item, dict) or item.get("reference_id") not in references:
                    raise APIError(400, "camera update refers to an unknown reference")
                if "camera" not in item:
                    raise APIError(400, "camera update must include camera")
                reference_id = item["reference_id"]
                if reference_id in seen:
                    raise APIError(400, "camera update repeats a reference")
                seen.add(reference_id)
                camera = None if item.get("camera") is None else self._normalize_reference_camera(item["camera"])
                if camera is None and "alignment_image_data_url" in item:
                    raise APIError(400, "alignment image requires a camera")
                reference = references[reference_id]
                if camera is not None:
                    original = (self.media_dir / reference["url"].rsplit("/", 1)[-1]).read_bytes()
                    size = self._image_dimensions(original)
                    intrinsics = camera["intrinsics"]
                    if size != (intrinsics["width"], intrinsics["height"]):
                        raise APIError(400, "camera image dimensions do not match the reference")
                alignment = None
                if "alignment_image_data_url" in item:
                    alignment = self._decode_image_data_url(item["alignment_image_data_url"], max_bytes=MAX_REFERENCE_BYTES)
                    if self._image_dimensions(alignment) != size:
                        raise APIError(400, "alignment image dimensions do not match the reference")
                prepared.append((reference, camera, alignment))
            for reference, camera, alignment in prepared:
                if camera is None:
                    reference.pop("camera", None)
                    reference.pop("alignment_image_url", None)
                else:
                    old_camera = reference.get("camera")
                    old_alignment_url = reference.get("alignment_image_url")
                    if reference.get("camera") != camera and alignment is None:
                        reference.pop("alignment_image_url", None)
                    reference["camera"] = camera
                    if alignment is not None:
                        old_alignment = self.media_dir / old_alignment_url.rsplit("/", 1)[-1] if isinstance(old_alignment_url, str) else None
                        if old_camera != camera or old_alignment is None or not old_alignment.is_file() or old_alignment.read_bytes() != alignment:
                            reference["alignment_image_url"] = self._write_media(alignment)
            if session.get("reference_clip") and seen & {frame["id"] for frame in dynamic_references}:
                session["reference_clip"]["views_revision"] = session["reference_clip"].get("views_revision", 0) + 1
            self._save()
            return {"session_id": session_id, "reference_images": copy.deepcopy(session["reference_images"]), "reference_clip": copy.deepcopy(session.get("reference_clip"))}

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.state["sessions"].get(session_id)
            if session is None:
                raise APIError(404, "session not found")
            return copy.deepcopy(session)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            return [copy.deepcopy(item) for item in self.state["sessions"].values()]

    def list_all_feedback(self) -> list[dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.state["feedback"])

    def ensure_workspace(self, project_dir: str | Path, *, preferred_session_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        """Bind this data directory to one project and one persistent browser session."""
        project = Path(project_dir).expanduser().resolve()
        if not project.is_dir():
            raise APIError(400, "workspace project directory does not exist")
        with self.lock:
            workspace = self.state.get("workspace")
            if workspace is not None:
                if workspace["project_dir"] != str(project):
                    raise APIError(409, "workspace is already bound to another project directory")
                if project_id is not None and workspace["project_id"] != project_id:
                    raise APIError(409, "workspace belongs to another registered scene")
                workspace.setdefault("created_thread_ids", [])
                workspace.setdefault("created_thread_specs", {})
                return copy.deepcopy(workspace)
            sessions = self.state["sessions"]
            if preferred_session_id is not None:
                session = sessions.get(preferred_session_id)
                if session is None or session["status"] != "open":
                    raise APIError(404, "requested workspace session is not open")
                session_id = preferred_session_id
            else:
                open_sessions = [item for item in sessions.values() if item["status"] == "open"]
                session_id = open_sessions[-1]["session_id"] if open_sessions else self.create_session()["session_id"]
            workspace = {
                "project_id": project_id or uuid.uuid4().hex,
                "project_dir": str(project),
                "session_id": session_id,
                "thread_id": None,
                "created_thread_ids": [],
                "created_thread_specs": {},
                "created_at": _now(),
                "agent": {"status": "disconnected", "turn_id": None, "error": None},
                "queue": [],
                "active_feedback_id": None,
                "approvals": [],
                "request_feedback": None,
                "events": [],
                "event_seq": 0,
            }
            self.state["workspace"] = workspace
            self._save()
            return copy.deepcopy(workspace)

    def workspace(self) -> dict[str, Any]:
        with self.lock:
            if "workspace" not in self.state:
                raise APIError(404, "workspace is not initialized")
            result = copy.deepcopy(self.state["workspace"])
            result["scene_revision"] = self.state["scene"]["revision"]
            return result

    def workspace_events(self, cursor: int = 0) -> dict[str, Any]:
        if type(cursor) is not int or cursor < 0:
            raise APIError(400, "cursor must be a nonnegative integer")
        with self.lock:
            workspace = self.state.get("workspace")
            if workspace is None:
                raise APIError(404, "workspace is not initialized")
            return {
                "items": copy.deepcopy([item for item in workspace["events"] if item["id"] > cursor]),
                "next_cursor": workspace["event_seq"],
            }

    def workspace_event(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        _safe_json(payload)
        with self.lock:
            workspace = self.state["workspace"]
            workspace["event_seq"] += 1
            event = {"id": workspace["event_seq"], "type": kind, "payload": copy.deepcopy(payload), "at": _now()}
            workspace["events"].append(event)
            workspace["events"] = workspace["events"][-500:]
            self._save()
            return copy.deepcopy(event)

    def workspace_thread(self, thread_id: str) -> None:
        if not isinstance(thread_id, str) or not thread_id:
            raise APIError(400, "invalid Codex thread ID")
        with self.lock:
            workspace = self.state["workspace"]
            old = workspace.get("thread_id")
            if old and old != thread_id:
                # App Server 0.156.1 may return a thread ID before persisting
                # a rollout. Its adapter recreates only a thread with no turn
                # attempt; the Gateway additionally requires no submitted
                # feedback before accepting and recording that replacement.
                if workspace["queue"] or any(item["session_id"] == workspace["session_id"] for item in self.state["feedback"]):
                    raise APIError(409, "workspace is already bound to another Codex thread")
            workspace["thread_id"] = thread_id
            self._save()
            if old and old != thread_id:
                self.workspace_event("empty_thread_recreated", {"old_thread_id": old, "thread_id": thread_id})

    def workspace_agent(self, *, status: str, turn_id: str | None = None, error: str | None = None) -> None:
        with self.lock:
            self.state["workspace"]["agent"] = {"status": status, "turn_id": turn_id, "error": error}
            self._save()

    def workspace_request_feedback(self, message: str, *, object_ids: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(message, str) or not 1 <= len(message) <= 2000:
            raise APIError(400, "request message must contain 1 to 2000 characters")
        if object_ids is not None and (not isinstance(object_ids, list) or len(object_ids) > 50 or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in object_ids)):
            raise APIError(400, "object_ids must contain at most 50 object IDs")
        with self.lock:
            request = {"message": message, "object_ids": object_ids or [], "at": _now(), "scene_revision": self.state["scene"]["revision"]}
            self.state["workspace"]["request_feedback"] = request
            self._save()
            self.workspace_event("feedback_requested", request)
            return copy.deepcopy(request)

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.state["sessions"].get(session_id)
            if session is None:
                raise APIError(404, "session not found")
            if session["status"] != "open":
                raise APIError(409, "session is already closed")
            session["status"] = "cancelled"
            session["closed_at"] = _now()
            self._save()
            return copy.deepcopy(session)

    @staticmethod
    def _screen_coordinates(value: Any, kind: str) -> dict[str, float]:
        if not isinstance(value, dict):
            raise APIError(400, "visual annotation needs normalized coordinates")
        required = {"x", "y"} | ({"x2", "y2"} if kind in {"rectangle", "rect", "line", "arrow"} else set())
        if not required.issubset(value) or set(value) - {"x", "y", "x2", "y2"}:
            raise APIError(400, "annotation coordinates need x/y and, for shapes, x2/y2")
        result = {}
        for key, number in value.items():
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 1:
                raise APIError(400, "annotation coordinates must be numbers between 0 and 1")
            result[key] = float(number)
        return result

    @staticmethod
    def _scene_node(value: Any, model_ids: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) - {"parent_object_id", "node_path", "node_name", "stable_id", "semantic_id"}:
            raise APIError(400, "scene_node must identify a model and child path")
        parent = value.get("parent_object_id")
        if not isinstance(parent, str) or parent not in model_ids:
            raise APIError(400, "scene_node parent must be a model object in the current scene")
        path = value.get("node_path")
        if not isinstance(path, list) or len(path) > 32 or any(type(index) is not int or not 0 <= index <= 65535 for index in path):
            raise APIError(400, "scene_node node_path must contain at most 32 child indices")
        name = value.get("node_name")
        if name is not None and (not isinstance(name, str) or len(name) > 160 or any(ord(char) < 32 for char in name)):
            raise APIError(400, "scene_node node_name must be short text")
        result = {"parent_object_id": parent, "node_path": list(path)}
        if name is not None:
            result["node_name"] = name
        for key in ("stable_id", "semantic_id"):
            if key in value:
                stable = value[key]
                if not isinstance(stable, str) or not 1 <= len(stable) <= 160 or any(ord(char) < 32 for char in stable):
                    raise APIError(400, f"scene_node {key} must be short text")
                result[key] = stable
        return result

    def _normalize_annotation(self, annotation: Any, object_ids: set[str], model_ids: set[str], reference_ids: set[str]) -> dict[str, Any]:
        if not isinstance(annotation, dict):
            raise APIError(400, "each annotation must be an object")
        kind = annotation.get("type", annotation.get("kind", annotation.get("operation")))
        visual_kinds = {"point", "rectangle", "rect", "line", "arrow", "text", "freehand"}
        legacy_kinds = {"target_box", "guide_line", "move", "resize", "note"}
        if kind not in visual_kinds | legacy_kinds:
            raise APIError(400, "unsupported annotation type")
        object_id = annotation.get("object_id")
        if object_id is not None and object_id not in object_ids:
            raise APIError(400, "annotation refers to an unknown object id")
        if kind in visual_kinds:
            pane = annotation.get("pane")
            if pane not in {"reference", "scene"}:
                raise APIError(400, "visual annotation pane must be reference or scene")
            reference_id = annotation.get("reference_image_id")
            if pane == "reference" and reference_id not in reference_ids:
                raise APIError(400, "annotation refers to an unknown reference image")
            if pane == "scene" and reference_id is not None:
                raise APIError(400, "scene annotation cannot name a reference image")
            coordinates = self._screen_coordinates(annotation.get("coordinates"), kind)
            normalized = copy.deepcopy(annotation)
            normalized["type"] = "rectangle" if kind == "rect" else kind
            normalized["coordinates"] = coordinates
            if kind == "freehand":
                points = annotation.get("points")
                if not isinstance(points, list) or not 2 <= len(points) <= 256:
                    raise APIError(400, "freehand annotation needs 2 to 256 points")
                normalized["points"] = [self._screen_coordinates(point, "point") for point in points]
            if "scene_node" in normalized:
                if pane != "scene":
                    raise APIError(400, "reference annotation cannot identify a scene node")
                normalized["scene_node"] = self._scene_node(normalized["scene_node"], model_ids)
                if object_id is not None and object_id != normalized["scene_node"]["parent_object_id"]:
                    raise APIError(400, "annotation object_id and scene_node parent disagree")
            for key in ("id", "group_id"):
                value = normalized.get(key)
                if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 64):
                    raise APIError(400, f"{key} must be short text")
            value = normalized.get("text")
            if value is not None and (not isinstance(value, str) or len(value) > 2000):
                raise APIError(400, "annotation text is too long")
        else:
            # Older spatial annotations remain readable for existing clients.
            if kind != "note" and object_id is None:
                raise APIError(400, "spatial annotation needs an object id")
            if annotation.get("coordinate_frame") not in (None, "world", "object_local"):
                raise APIError(400, "coordinate_frame must be world or object_local")
            required_vectors = {"target_box": ("center", "size"), "guide_line": ("start", "end"), "move": ("target_position",), "resize": ("target_size",), "note": ()}[kind]
            for vector_name in required_vectors:
                _vector(annotation.get(vector_name), vector_name, positive=vector_name in {"size", "target_size"})
            if kind == "target_box" and annotation.get("anchor", "center") not in {"bottom", "center"}:
                raise APIError(400, "target_box anchor must be bottom or center")
            if kind == "target_box" and "source_box" in annotation:
                source_box = annotation["source_box"]
                if not isinstance(source_box, dict):
                    raise APIError(400, "source_box must contain center and size")
                _vector(source_box.get("center"), "source_box.center")
                if any(value < 0 for value in _vector(source_box.get("size"), "source_box.size")):
                    raise APIError(400, "source_box.size cannot be negative")
            normalized = copy.deepcopy(annotation)
            normalized.pop("operation", None)
            normalized["type"] = kind
        _safe_json(normalized)
        return normalized

    def _prepare_image_refs(self, session: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate independently frozen images before writing any evidence."""
        from dynamic import number, reference_views, shared_duration
        from PIL import Image, ImageOps

        refs = payload.get("image_refs", [])
        if not isinstance(refs, list) or len(refs) > MAX_IMAGE_REFS:
            raise APIError(400, "image_refs must contain at most 8 dragged images")
        clip = session.get("reference_clip")
        views = {view["clip_id"]: view for view in reference_views(clip)}
        static = {ref["id"]: ref for ref in session.get("reference_images", [])}
        dynamic = {frame["id"]: (view, index, frame) for view in views.values() for index, frame in enumerate(view["frames"])}
        objects = {obj["id"]: obj for obj in self.state["scene"]["objects"]}
        allowed = {"id", "pane", "label", "original_data_url", "annotated_data_url", "image_width", "image_height",
                   "scene_revision", "camera", "reference_id", "clip_id", "view_id", "time_sec", "frame_index",
                   "selected_object_ids", "selected_scene_nodes"}
        seen: set[str] = set()
        prepared = []

        def dimensions(data: bytes) -> tuple[int, int]:
            with Image.open(io.BytesIO(data)) as image:
                return ImageOps.exif_transpose(image).size

        for item in refs:
            if not isinstance(item, dict) or set(item) - allowed:
                raise APIError(400, "dragged image contains unsupported fields")
            image_id = item.get("id")
            if not isinstance(image_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", image_id) or image_id in seen:
                raise APIError(400, "image_refs IDs must be URL-safe and unique")
            seen.add(image_id)
            pane, label = item.get("pane"), item.get("label")
            if not isinstance(pane, str) or pane not in {"reference", "scene"} or not isinstance(label, str) or not 1 <= len(label.strip()) <= 180 or any(ord(char) < 32 for char in label):
                raise APIError(400, "dragged image needs a pane and short label")
            width, height = item.get("image_width"), item.get("image_height")
            if type(width) is not int or type(height) is not int or not 1 <= width <= 32768 or not 1 <= height <= 32768:
                raise APIError(400, "dragged image dimensions must be positive integers")
            original = self._decode_image_data_url(item.get("original_data_url"))
            if dimensions(original) != (width, height):
                raise APIError(400, "dragged image dimensions do not match its captured pixels")
            images = {}
            if item.get("annotated_data_url") is not None:
                annotated = self._decode_image_data_url(item["annotated_data_url"])
                if dimensions(annotated) != (width, height):
                    raise APIError(400, "annotated dragged image dimensions do not match its original capture")
                images["annotated"] = annotated
            result: dict[str, Any] = {"id": image_id, "pane": pane, "label": label.strip(), "image_width": width, "image_height": height}
            reference_id = item.get("reference_id")
            if reference_id is not None and (not isinstance(reference_id, str) or reference_id not in static and reference_id not in dynamic):
                raise APIError(400, "dragged image reference_id must belong to this session's current sources")
            if reference_id is not None:
                result["reference_id"] = reference_id
            if pane == "reference":
                if reference_id is None:
                    raise APIError(400, "reference dragged image requires reference_id")
                if any(key in item for key in ("scene_revision", "camera", "selected_object_ids", "selected_scene_nodes")):
                    raise APIError(400, "reference dragged image cannot contain scene capture metadata")
                if reference_id in dynamic:
                    view, index, reference = dynamic[reference_id]
                    if item.get("clip_id") != clip["clip_id"] or item.get("view_id") != view["clip_id"]:
                        raise APIError(400, "dragged reference clip/view does not match its exact source frame")
                    time_sec = number(item.get("time_sec"), "dragged reference time_sec")
                    if abs(time_sec - reference["time_sec"]) > 1e-6:
                        raise APIError(400, "dragged reference time_sec does not match its exact source frame")
                    if "frame_index" in item and (type(item["frame_index"]) is not int or item["frame_index"] != index):
                        raise APIError(400, "dragged reference frame_index does not match its source ordinal")
                    result.update(clip_id=clip["clip_id"], view_id=view["clip_id"], view_name=view["name"], time_sec=time_sec, frame_index=index)
                else:
                    reference = static[reference_id]
                    if any(key in item for key in ("clip_id", "view_id", "time_sec", "frame_index")):
                        raise APIError(400, "static dragged reference cannot name a clip frame")
                source_path = self.media_dir / reference["url"].rsplit("/", 1)[-1]
                if not source_path.is_file():
                    raise APIError(409, "dragged reference original source is missing")
                source_width, source_height = dimensions(source_path.read_bytes())
                if abs(width * source_height - height * source_width) > 2 * max(source_width, source_height):
                    raise APIError(400, "dragged reference capture must preserve the source aspect ratio")
                result.update(original_url=reference["url"], reference_name=reference["name"],
                              source_image_width=source_width, source_image_height=source_height)
                if reference.get("camera"):
                    result["reference_camera"] = copy.deepcopy(reference["camera"])
                images["display_original"] = original
            else:
                revision = item.get("scene_revision")
                current_revision = self.state["scene"]["revision"]
                if type(revision) is not int or not 1 <= revision <= current_revision:
                    raise APIError(400, "dragged scene revision must identify an already published scene")
                stale = revision != current_revision
                if stale and payload.get("confirm_stale") is not True:
                    raise APIError(409, "confirm stale dragged scene evidence before submitting",
                                   detail={"code": "feedback_revision_conflict", "current_scene_revision": current_revision})
                camera = item.get("camera")
                _safe_json(camera)
                if not isinstance(camera, dict) or not camera or len(json.dumps(camera)) > 16_000:
                    raise APIError(400, "dragged scene camera must be a small nonempty object")
                vectors = {key: _vector(camera.get(key), "dragged scene camera." + key)
                           for key in ("position", "target", "up")}
                if vectors["position"] == vectors["target"] or not any(vectors["up"]):
                    raise APIError(400, "dragged scene camera needs a distinct target and nonzero up vector")
                number(camera.get("fov"), "dragged scene camera.fov", minimum=.001, maximum=179.999)
                number(camera.get("aspect"), "dragged scene camera.aspect", minimum=.0001, maximum=10000)
                selected = item.get("selected_object_ids", [])
                if not isinstance(selected, list) or len(selected) > 100 or any(not isinstance(value, str) or not ID_RE.fullmatch(value) or not stale and value not in objects for value in selected):
                    raise APIError(400, "dragged image selection contains invalid object IDs")
                if len(set(selected)) != len(selected):
                    raise APIError(400, "dragged image selected IDs must be unique")
                models = {key for key, obj in objects.items() if obj["type"] == "model"}
                if stale:
                    models.update(selected)
                nodes = item.get("selected_scene_nodes", [])
                if not isinstance(nodes, list) or len(nodes) > 64:
                    raise APIError(400, "dragged image selected_scene_nodes must contain at most 64 nodes")
                nodes = [self._scene_node(node, models) for node in nodes]
                if any(node["parent_object_id"] not in selected for node in nodes) or len({(node["parent_object_id"], tuple(node["node_path"])) for node in nodes}) != len(nodes):
                    raise APIError(400, "dragged image nodes must be unique and belong to its selection")
                result.update(scene_revision=revision, camera=copy.deepcopy(camera), selected_object_ids=list(selected), selected_scene_nodes=nodes)
                if stale:
                    result["from_stale_snapshot"] = True
                if any(key in item for key in ("clip_id", "view_id")):
                    if not clip or item.get("clip_id") != clip["clip_id"] or not isinstance(item.get("view_id"), str) or item["view_id"] not in views:
                        raise APIError(400, "dragged scene clip/view must match the current session")
                    if reference_id in dynamic and dynamic[reference_id][0]["clip_id"] != item["view_id"]:
                        raise APIError(400, "dragged scene view does not match its reference frame")
                    result.update(clip_id=clip["clip_id"], view_id=item["view_id"], view_name=views[item["view_id"]]["name"])
                if "time_sec" in item:
                    result["time_sec"] = number(item["time_sec"], "dragged scene time_sec", maximum=shared_duration(clip) if clip else 86400)
                if "frame_index" in item:
                    raise APIError(400, "scene screenshot frame_index is not a reference frame ordinal")
                images["original"] = original
            _safe_json(result)
            prepared.append({"image": result, "images": images})
        return prepared

    @staticmethod
    def _inline_references(note: str, scene_objects: dict[str, dict[str, Any]],
                           object_ids: set[str], annotations: list[dict[str, Any]],
                           referenced_scene_nodes: list[dict[str, Any]],
                           *, stale_snapshot: bool, image_refs: list[dict[str, Any]] | None = None,
                           scene_refs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Resolve tokens in the user's prose against this frozen submission."""
        matches = list(INLINE_REFERENCE_RE.finditer(note))
        matched_starts = {match.start() for match in matches}
        if any(match.start() not in matched_starts for match in INLINE_REFERENCE_START_RE.finditer(note)):
            raise APIError(400, "invalid inline reference; use object, annotation, node, image or scene tokens")
        images_by_id = {item["id"]: item for item in image_refs or []}
        used_images: set[str] = set()
        scenes_by_id = {item["id"]: item for item in scene_refs or []}
        used_scenes: set[str] = set()
        annotations_by_id: dict[str, list[dict[str, Any]]] = {}
        for annotation in annotations:
            annotation_id = annotation.get("id")
            if isinstance(annotation_id, str):
                annotations_by_id.setdefault(annotation_id, []).append(annotation)
        nodes_by_path = {
            (node["parent_object_id"], tuple(node["node_path"])): node
            for node in referenced_scene_nodes
        }
        resolved = []
        seen: set[tuple[str, str]] = set()
        used_nodes: set[tuple[str, tuple[int, ...]]] = set()
        for match in matches:
            kind = match.group("simple_kind") or "node"
            reference_id = match.group("simple_id") if kind != "node" else f'{match.group("node_object")}:{match.group("node_path")}'
            key = (kind, reference_id)
            if key in seen:
                continue
            seen.add(key)
            entry: dict[str, Any] = {"token": match.group(), "kind": kind, "id": reference_id}
            if kind == "object":
                if reference_id not in object_ids:
                    raise APIError(400, f"inline object reference {reference_id} is not in this scene")
                scene_object = scene_objects.get(reference_id)
                if scene_object is None:
                    if not stale_snapshot:
                        raise APIError(400, f"inline object reference {reference_id} is not in this scene")
                    entry["object"] = {"id": reference_id}
                    entry["from_stale_snapshot"] = True
                else:
                    entry["object"] = copy.deepcopy(scene_object)
            elif kind == "annotation":
                candidates = annotations_by_id.get(reference_id, [])
                if len(candidates) != 1:
                    detail = "ambiguous" if candidates else "unknown"
                    raise APIError(400, f"inline annotation reference {reference_id} is {detail}")
                entry["annotation"] = copy.deepcopy(candidates[0])
            elif kind == "image":
                if reference_id not in images_by_id:
                    raise APIError(400, f"inline image reference {reference_id} has no submitted frozen image")
                used_images.add(reference_id)
                entry["image"] = copy.deepcopy(images_by_id[reference_id])
            elif kind == "scene":
                if reference_id not in scenes_by_id:
                    raise APIError(400, f"inline scene reference {reference_id} has no archived scene")
                used_scenes.add(reference_id)
                reference = scenes_by_id[reference_id]
                entry["scene_reference"] = {key: reference[key] for key in ("id", "name", "source_project_id", "source_scene_revision", "read_only", "snapshot_json_path")}
            else:
                node_object = match.group("node_object")
                node_path_text = match.group("node_path")
                node_path = tuple(int(index) for index in node_path_text.split("/"))
                node_key = (node_object, node_path)
                node = nodes_by_path.get(node_key)
                if node is None or "/".join(str(index) for index in node_path) != node_path_text:
                    raise APIError(400, f"inline node reference {reference_id} has no matching submitted scene node")
                used_nodes.add(node_key)
                entry["scene_node"] = copy.deepcopy(node)
                if scene_objects.get(node_object, {}).get("type") != "model":
                    entry["from_stale_snapshot"] = True
            resolved.append(entry)
        if used_nodes != set(nodes_by_path):
            raise APIError(400, "referenced_scene_nodes contains a node not cited in note")
        if used_images != set(images_by_id):
            raise APIError(400, "image_refs contains an image not cited in note")
        if used_scenes != set(scenes_by_id):
            raise APIError(400, "scene_refs contains a scene not cited in note")
        return resolved

    def submit_feedback(self, session_id: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise APIError(400, "feedback must be a JSON object")
        with self.lock:
            session = self.state["sessions"].get(session_id)
            if session is None:
                raise APIError(404, "session not found")
            if session["status"] != "open":
                raise APIError(409, "session is already closed")
            client_key = payload.get("idempotency_key")
            digest = None
            if client_key is not None:
                if not isinstance(client_key, str) or not CLIENT_KEY_RE.fullmatch(client_key):
                    raise APIError(400, "idempotency_key must be 8 to 128 URL-safe characters")
                try:
                    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise APIError(400, "submission contains invalid JSON values") from exc
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                for old in self.state["feedback"]:
                    if old["session_id"] == session_id and old.get("idempotency_key") == client_key:
                        if old.get("payload_digest") != digest:
                            raise APIError(409, "idempotency_key was already used for another submission")
                        return copy.deepcopy(old)
            revision = payload.get("scene_revision")
            if type(revision) is not int or revision < 1:
                raise APIError(400, "scene_revision must be a positive integer")
            if revision > self.state["scene"]["revision"]:
                raise APIError(400, "scene_revision cannot be newer than the current scene")
            stale_snapshot = revision != self.state["scene"]["revision"]
            if stale_snapshot and payload.get("confirm_stale") is not True:
                raise APIError(409, f"scene revision changed to {self.state['scene']['revision']}; reload and resubmit",
                               detail={"code": "feedback_revision_conflict", "current_scene_revision": self.state["scene"]["revision"]})
            annotations = payload.get("annotations", [])
            if not isinstance(annotations, list) or len(annotations) > 100:
                raise APIError(400, "annotations must be an array with at most 100 items")
            scene_objects = {obj["id"]: obj for obj in self.state["scene"]["objects"]}
            object_ids = set(scene_objects)
            model_ids = {obj["id"] for obj in self.state["scene"]["objects"] if obj["type"] == "model"}
            if stale_snapshot:
                # Historic IDs are only visual references; no scene edit is inferred here.
                historic_selected = payload.get("selected_object_ids", [])
                if isinstance(historic_selected, list):
                    object_ids.update(item for item in historic_selected if isinstance(item, str) and ID_RE.fullmatch(item))
                object_ids.update(item.get("object_id") for item in annotations if isinstance(item, dict) and isinstance(item.get("object_id"), str) and ID_RE.fullmatch(item["object_id"]))
                historic_prompts = payload.get("object_prompts", [])
                if isinstance(historic_prompts, list):
                    object_ids.update(item.get("object_id") for item in historic_prompts if isinstance(item, dict) and isinstance(item.get("object_id"), str) and ID_RE.fullmatch(item["object_id"]))
                historic_nodes = payload.get("selected_scene_nodes", [])
                if isinstance(historic_nodes, list):
                    model_ids.update(node.get("parent_object_id") for node in historic_nodes if isinstance(node, dict) and isinstance(node.get("parent_object_id"), str) and ID_RE.fullmatch(node["parent_object_id"]))
                historic_referenced_nodes = payload.get("referenced_scene_nodes", [])
                if isinstance(historic_referenced_nodes, list):
                    model_ids.update(node.get("parent_object_id") for node in historic_referenced_nodes if isinstance(node, dict) and isinstance(node.get("parent_object_id"), str) and ID_RE.fullmatch(node["parent_object_id"]))
                for frame in [*payload.get("dynamic_frames", []), *payload.get("scene_snapshots", [])] if isinstance(payload.get("dynamic_frames", []), list) and isinstance(payload.get("scene_snapshots", []), list) else []:
                    if not isinstance(frame, dict):
                        continue
                    selected = frame.get("selected_object_ids", [])
                    if isinstance(selected, list):
                        object_ids.update(item for item in selected if isinstance(item, str) and ID_RE.fullmatch(item))
                    nodes = frame.get("selected_scene_nodes", [])
                    if isinstance(nodes, list):
                        model_ids.update(node.get("parent_object_id") for node in nodes if isinstance(node, dict) and isinstance(node.get("parent_object_id"), str) and ID_RE.fullmatch(node["parent_object_id"]))
                    clips = frame.get("animation_clips", [])
                    if isinstance(clips, list):
                        model_ids.update(item.get("object_id") for item in clips if isinstance(item, dict) and isinstance(item.get("object_id"), str) and ID_RE.fullmatch(item["object_id"]))
            from dynamic import reference_frames, reference_clip_metadata
            clip_references = reference_frames(session.get("reference_clip"))
            references_by_id = {image["id"]: image for image in session.get("reference_images", []) + clip_references}
            reference_ids = set(references_by_id)
            active_reference_id = payload.get("active_reference_id")
            aligned_reference_id = payload.get("aligned_reference_id")
            if active_reference_id is not None and active_reference_id not in reference_ids:
                raise APIError(400, "active_reference_id must identify a current reference")
            if aligned_reference_id is not None and (aligned_reference_id not in reference_ids or "camera" not in references_by_id[aligned_reference_id]):
                raise APIError(400, "aligned_reference_id must identify a calibrated reference")
            normalized_annotations = [self._normalize_annotation(item, object_ids, model_ids, reference_ids) for item in annotations]
            from dynamic import prepare_dynamic_feedback, validate_timed_annotations, prepare_scene_snapshots, validate_snapshot_annotations
            timeline, dynamic_frames = prepare_dynamic_feedback(self, session, payload, revision, object_ids, model_ids, require_oldest=not payload.get("scene_snapshots"))
            validate_timed_annotations(normalized_annotations, timeline, dynamic_frames)
            scene_snapshots = prepare_scene_snapshots(self, session, payload, revision, object_ids, model_ids)
            if scene_snapshots:
                validate_snapshot_annotations(normalized_annotations, scene_snapshots)
                if {item["frame"]["id"] for item in scene_snapshots} & {item["frame"]["id"] for item in dynamic_frames}:
                    raise APIError(400, "scene snapshots and dynamic frames must have distinct ids")
                if min(item["frame"]["scene_revision"] for item in [*scene_snapshots, *dynamic_frames]) != revision:
                    raise APIError(409, "scene_revision must be the oldest saved evidence revision",
                                   detail={"code": "feedback_revision_conflict", "current_scene_revision": self.state["scene"]["revision"]})
            referenced_scene_nodes = payload.get("referenced_scene_nodes", [])
            if not isinstance(referenced_scene_nodes, list) or len(referenced_scene_nodes) > 64:
                raise APIError(400, "referenced_scene_nodes must be an array of at most 64 nodes")
            referenced_scene_nodes = [self._scene_node(node, model_ids) for node in referenced_scene_nodes]
            referenced_node_keys = {(node["parent_object_id"], tuple(node["node_path"])) for node in referenced_scene_nodes}
            if len(referenced_node_keys) != len(referenced_scene_nodes):
                raise APIError(400, "referenced_scene_nodes must be unique")
            note = payload.get("note", "")
            if not isinstance(note, str) or len(note) > 10_000:
                raise APIError(400, "note must be text up to 10000 characters")
            prepared_images = self._prepare_image_refs(session, payload)
            from scene_references import prepare_scene_refs, publish_scene_refs
            prepared_scene_refs = prepare_scene_refs(self, session_id, payload, note)
            inline_references = self._inline_references(
                note, scene_objects, object_ids, normalized_annotations, referenced_scene_nodes,
                stale_snapshot=stale_snapshot,
                image_refs=[item["image"] for item in prepared_images],
                scene_refs=[item["reference"] for item in prepared_scene_refs],
            )
            object_prompts = payload.get("object_prompts", [])
            if not isinstance(object_prompts, list) or len(object_prompts) > 24:
                raise APIError(400, "object_prompts must be an array with at most 24 items")
            normalized_prompts = []
            for item in object_prompts:
                if not isinstance(item, dict) or set(item) - {"object", "prompt", "object_id"}:
                    raise APIError(400, "each object prompt needs object and prompt text")
                name, prompt = item.get("object"), item.get("prompt")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 180:
                    raise APIError(400, "object prompt object must be 1 to 180 characters")
                if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
                    raise APIError(400, "object prompt text must be 1 to 2000 characters")
                normalized = {"object": name.strip(), "prompt": prompt.strip()}
                object_id = item.get("object_id")
                if object_id is not None:
                    if not isinstance(object_id, str) or object_id not in object_ids:
                        raise APIError(400, "object prompt refers to an unknown object id")
                    normalized["object_id"] = object_id
                normalized_prompts.append(normalized)
            if not annotations and not note.strip() and not normalized_prompts and not session.get("reference_images") and not dynamic_frames and not scene_snapshots and not payload.get("pose_refs") and not payload.get("pose_edits"):
                raise APIError(400, "add a reference, annotation, object prompt or note before submitting")
            camera = payload.get("camera")
            if camera is not None:
                _safe_json(camera)
                if not isinstance(camera, dict) or len(json.dumps(camera, ensure_ascii=False)) > 16_000:
                    raise APIError(400, "camera must be a small object")
            selected_ids = payload.get("selected_object_ids", [])
            if not isinstance(selected_ids, list) or len(selected_ids) > 100 or any(not isinstance(item, str) or item not in object_ids for item in selected_ids):
                raise APIError(400, "selected_object_ids contains an unknown object")
            if len(selected_ids) != len(set(selected_ids)):
                raise APIError(400, "selected_object_ids must be unique")
            selected_scene_nodes = payload.get("selected_scene_nodes", [])
            if not isinstance(selected_scene_nodes, list) or len(selected_scene_nodes) > 64:
                raise APIError(400, "selected_scene_nodes must be an array of at most 64 nodes")
            selected_scene_nodes = [self._scene_node(node, model_ids) for node in selected_scene_nodes]
            if any(node["parent_object_id"] not in selected_ids for node in selected_scene_nodes):
                raise APIError(400, "selected_scene_nodes parent must also be selected_object_ids")
            node_keys = {(node["parent_object_id"], tuple(node["node_path"])) for node in selected_scene_nodes}
            if len(node_keys) != len(selected_scene_nodes):
                raise APIError(400, "selected_scene_nodes must be unique")
            annotated_refs = payload.get("reference_annotated_data_urls", [])
            if not isinstance(annotated_refs, list) or len(annotated_refs) > MAX_REFERENCES:
                raise APIError(400, "reference_annotated_data_urls must be an array of at most 8 images")
            prepared_refs = []
            seen_refs = set()
            for item in annotated_refs:
                if not isinstance(item, dict) or item.get("reference_id") not in reference_ids or item["reference_id"] in seen_refs:
                    raise APIError(400, "annotated reference has an unknown or duplicate id")
                seen_refs.add(item["reference_id"])
                prepared_refs.append((item["reference_id"], self._decode_image_data_url(item.get("data_url"))))
            prepared_scene = {}
            for field in ("scene_original_data_url", "scene_annotated_data_url"):
                if field in payload and payload[field] is not None:
                    prepared_scene[field] = self._decode_image_data_url(payload[field])
            if "screenshot_data_url" in payload and payload["screenshot_data_url"] is not None and "scene_annotated_data_url" not in prepared_scene:
                prepared_scene["scene_annotated_data_url"] = self._decode_image_data_url(payload["screenshot_data_url"])
            crops = payload.get("crops", [])
            from comparison import prepare_comparison
            comparison_fields, comparison_images = prepare_comparison(self, session, payload)
            from human_pose import prepare_pose_feedback, prepare_pose_edit_feedback, _atomic_json
            prepared_pose = prepare_pose_feedback(self, session_id, payload.get("pose_refs"), note)
            prepared_pose_edits = prepare_pose_edit_feedback(self, session_id, payload)
            if not isinstance(crops, list) or len(crops) > MAX_CROPS:
                raise APIError(400, "crops must be an array of at most 8 images")
            prepared_crops = []
            for crop in crops:
                if not isinstance(crop, dict) or crop.get("source") not in {"reference", "scene"}:
                    raise APIError(400, "crop source must be reference or scene")
                ref_id = crop.get("reference_id")
                if crop["source"] == "reference" and ref_id not in reference_ids:
                    raise APIError(400, "crop reference id is unknown")
                if crop["source"] == "scene" and ref_id is not None:
                    raise APIError(400, "scene crop cannot name a reference image")
                prepared_crops.append((crop["source"], ref_id, self._decode_image_data_url(crop.get("data_url"))))
            feedback = {"feedback_id": uuid.uuid4().hex, "session_id": session_id, "scene_revision": revision, "submitted_at": _now(), "annotations": normalized_annotations, "note": note, "inline_references": inline_references, "object_prompts": normalized_prompts, "reference_images": copy.deepcopy(session.get("reference_images", [])), "selected_object_ids": selected_ids, "selected_scene_nodes": selected_scene_nodes, "referenced_scene_nodes": referenced_scene_nodes}
            if prepared_scene_refs:
                feedback["scene_refs"] = publish_scene_refs(self, prepared_scene_refs)
            if prepared_images:
                feedback["image_refs"] = [{**item["image"], **{name + "_url": self._write_media(data) for name, data in item["images"].items()}} for item in prepared_images]
                saved_images = {item["id"]: item for item in feedback["image_refs"]}
                for inline in feedback["inline_references"]:
                    if inline["kind"] == "image":
                        inline["image"] = copy.deepcopy(saved_images[inline["id"]])
            if prepared_pose:
                feedback["human_pose"] = [{**{key: value for key, value in item.items() if key != "_overlay_data"},
                                           "pose_overlay_url": self._write_media(item["_overlay_data"])} for item in prepared_pose]
            if prepared_pose_edits:
                feedback["human_pose_edits"] = []
                corrections_dir = self.data_dir / "human_pose" / "corrections" / feedback["feedback_id"]
                corrections_dir.mkdir(parents=True, exist_ok=False)
                for item in prepared_pose_edits:
                    correction_path = corrections_dir / (item["id"] + ".json")
                    _atomic_json(correction_path, item["document"])
                    parent_dir = self.data_dir / "human_pose" / item["parent_job_id"]
                    parent_paths = {key: str(path.resolve()) for key, path in (
                        ("source_manifest_path", parent_dir / "input.json"),
                        ("parent_result_path", parent_dir / "output.json"),
                    ) if path.is_file()}
                    feedback["human_pose_edits"].append({
                        **{key: value for key, value in item.items() if key != "_overlay_data"},
                        **parent_paths,
                        "pose_overlay_url": self._write_media(item["_overlay_data"]),
                        "corrections_path": str(correction_path.resolve()),
                        "corrections_url": f"/api/workspace/pose/corrections/{feedback['feedback_id']}/{item['id']}",
                    })
            feedback.update(comparison_fields)
            feedback.update({key + "_url": self._write_media(data) for key, data in comparison_images.items()})
            if scene_snapshots:
                feedback["scene_snapshots"] = [{**item["frame"], **{field + "_url": self._write_media(data) for field, data in item["images"].items()}} for item in scene_snapshots]
            if timeline is not None:
                feedback["timeline"] = timeline
                feedback["dynamic_frames"] = [{**item["frame"], **{field + "_url": self._write_media(data) for field, data in item["images"].items()}} for item in dynamic_frames]
                if session.get("reference_clip"):
                    feedback["reference_clip"] = reference_clip_metadata(session["reference_clip"])
            if active_reference_id is not None:
                feedback["active_reference_id"] = active_reference_id
            if aligned_reference_id is not None:
                feedback["aligned_reference_id"] = aligned_reference_id
            if client_key is not None:
                feedback["idempotency_key"] = client_key
                feedback["payload_digest"] = digest
            if stale_snapshot:
                feedback["submitted_from_stale_snapshot"] = True
            if camera is not None:
                feedback["camera"] = camera
            feedback["reference_annotated_images"] = [{"reference_id": ref_id, "url": self._write_media(data)} for ref_id, data in prepared_refs]
            if "scene_original_data_url" in prepared_scene:
                feedback["scene_original_url"] = self._write_media(prepared_scene["scene_original_data_url"])
            if "scene_annotated_data_url" in prepared_scene:
                feedback["scene_annotated_url"] = self._write_media(prepared_scene["scene_annotated_data_url"])
                feedback["screenshot_url"] = feedback["scene_annotated_url"]
            feedback["crops"] = [{"source": source, "reference_id": ref_id, "url": self._write_media(data)} for source, ref_id, data in prepared_crops]
            self.state["feedback"].append(feedback)
            session["last_submitted_at"] = feedback["submitted_at"]
            session["feedback_count"] = session.get("feedback_count", 0) + 1
            workspace = self.state.get("workspace")
            if workspace and workspace["session_id"] == session_id:
                queue_item = {"feedback_id": feedback["feedback_id"], "scene_revision": revision, "status": "queued", "enqueued_at": feedback["submitted_at"], "confirmed_against_revision": self.state["scene"]["revision"] if payload.get("confirm_stale") is True else None, "turn_id": None, "error": None, "target_thread_id": workspace.get("thread_id")}
                if workspace.get("feedback_transport") == "mcp_events":
                    queue_item.update(feedback_transport="mcp_events", status="event_pending")
                workspace["queue"].append(queue_item)
                workspace["request_feedback"] = None
                workspace["event_seq"] += 1
                object_prompts_summary = "\n".join(f"{item['object']}：{item['prompt']}" for item in normalized_prompts)
                if len(object_prompts_summary) > 4000:
                    object_prompts_summary = object_prompts_summary[:3999] + "…"
                workspace["events"].append({"id": workspace["event_seq"], "type": "feedback_queued", "payload": {"feedback_id": feedback["feedback_id"], "scene_revision": revision, "note": note[:4000], "object_prompts_summary": object_prompts_summary}, "at": _now()})
                workspace["events"] = workspace["events"][-500:]
            self._save()
            return copy.deepcopy(feedback)

    def feedback(self, session_id: str, cursor: int = 0) -> dict[str, Any]:
        with self.lock:
            session = self.get_session(session_id)
            if type(cursor) is not int or cursor < 0:
                raise APIError(400, "cursor must be a nonnegative integer")
            items = [copy.deepcopy(item) for item in self.state["feedback"] if item["session_id"] == session_id]
            return {"session_id": session_id, "status": session["status"], "items": items[cursor:], "next_cursor": len(items)}

    def feedback_by_id(self, feedback_id: str) -> dict[str, Any]:
        with self.lock:
            item = next((entry for entry in self.state["feedback"] if entry["feedback_id"] == feedback_id), None)
            if item is None:
                raise APIError(404, "feedback not found")
            return copy.deepcopy(item)

    @staticmethod
    def _validate_glb_source(local_path: str) -> Path:
        if not isinstance(local_path, str) or not local_path:
            raise APIError(400, "local_path must be a file path")
        try:
            source = Path(local_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise APIError(400, "model file does not exist") from exc
        if not source.is_file() or source.suffix.lower() != ".glb":
            raise APIError(400, "model must be a .glb file")
        try:
            file_size = source.stat().st_size
        except OSError as exc:
            raise APIError(400, "cannot read model file") from exc
        if not 20 <= file_size <= MAX_MODEL_BYTES:
            raise APIError(400, "GLB must be between 20 bytes and 100 MB")
        try:
            with source.open("rb") as handle:
                header = handle.read(12)
                magic, version, declared_size = struct.unpack("<4sII", header)
                if magic != b"glTF" or version != 2 or declared_size != file_size:
                    raise APIError(400, "file is not a valid GLB 2.0 container")
                remaining = file_size - 12
                chunk_number = 0
                bin_start = None
                bin_length = 0
                document = None
                while remaining:
                    if remaining < 8:
                        raise APIError(400, "GLB has a truncated chunk header")
                    chunk_size, chunk_type = struct.unpack("<I4s", handle.read(8))
                    if chunk_size % 4 or chunk_size > remaining - 8:
                        raise APIError(400, "GLB has an invalid chunk length")
                    if chunk_number == 0:
                        if chunk_type != b"JSON" or not 0 < chunk_size <= 16 * 1024 * 1024:
                            raise APIError(400, "GLB has an invalid JSON chunk")
                        json_bytes = handle.read(chunk_size)
                        document = json.loads(json_bytes.decode("utf-8"), parse_constant=_reject_glb_json_constant)
                    elif chunk_type == b"BIN\x00":
                        if chunk_number != 1 or bin_start is not None:
                            raise APIError(400, "GLB BIN chunk must occur only as the second chunk")
                        bin_start = handle.tell()
                        bin_length = chunk_size
                        handle.seek(chunk_size, os.SEEK_CUR)
                    else:
                        if chunk_type == b"JSON":
                            raise APIError(400, "GLB has a duplicate JSON chunk")
                        # Unknown chunks are allowed after JSON/BIN by glTF 2.0.
                        handle.seek(chunk_size, os.SEEK_CUR)
                    remaining -= 8 + chunk_size
                    chunk_number += 1
                if document is None:
                    raise APIError(400, "GLB is missing its JSON chunk")
                _validate_glb_document(document, handle, bin_start, bin_length)
        except (OSError, struct.error, UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
            raise APIError(400, "file is not a valid GLB 2.0 container") from exc
        return source

    def _copy_glb(self, source: Path) -> tuple[str, str]:
        file_name = f"{uuid.uuid4().hex}.glb"
        destination = self.assets_dir / file_name
        hasher = hashlib.sha256()
        try:
            with source.open("rb") as src, destination.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    hasher.update(chunk)
                    dst.write(chunk)
            # The source could have changed after its first validation. Only the
            # exact bytes that will be served may be committed to scene state.
            self._validate_glb_source(str(destination))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return f"/assets/{file_name}", hasher.hexdigest()

    def set_scene_preview(self, local_path: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        """Atomically replace the visible result with one stable selectable GLB."""
        source = self._validate_glb_source(local_path)
        with self.lock:
            if expected_revision is not None:
                self._check_revision(expected_revision)
            url, digest = self._copy_glb(source)
            obj = self._validate_object({"id": "scene_preview", "name": source.stem[:120] or "Current scene", "type": "model", "url": url, "position": [0, 0, 0], "size": [1, 1, 1], "rotation": [0, 0, 0], "color": "#ffffff", "metadata": {"source_name": source.name, "sha256": digest}})
            scene = self.state["scene"]
            scene["objects"] = [obj]
            scene["revision"] += 1
            self._save()
            if "workspace" in self.state:
                self.workspace_event("scene_published", {"scene_revision": scene["revision"], "asset_url": url, "sha256": digest})
            return copy.deepcopy(scene)

    def import_model(self, local_path: str, *, object_id: str | None = None, name: str | None = None, position: list[float] | None = None, size: list[float] | None = None) -> dict[str, Any]:
        source = self._validate_glb_source(local_path)
        object_id = object_id or f"model_{uuid.uuid4().hex[:10]}"
        name = name or source.stem[:120]
        if not isinstance(object_id, str) or not ID_RE.fullmatch(object_id):
            raise APIError(400, "object id must start with a letter and use letters, numbers, '_' or '-'")
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise APIError(400, "object name must contain 1 to 120 characters")
        position = _vector(position if position is not None else [0, 0, 0], "position")
        size = _vector(size if size is not None else [1, 1, 1], "size", positive=True)
        with self.lock:
            if any(obj["id"] == object_id for obj in self.state["scene"]["objects"]):
                raise APIError(409, "object id already exists")
            url, digest = self._copy_glb(source)
            obj = self._validate_object({"id": object_id, "name": name, "type": "model", "url": url, "position": position or [0, 0, 0], "size": size or [1, 1, 1], "rotation": [0, 0, 0], "color": "#ffffff", "metadata": {"source_name": source.name, "sha256": digest}})
            scene = self.state["scene"]
            scene["objects"].append(obj)
            scene["revision"] += 1
            self._save()
            return {"object": copy.deepcopy(obj), "scene_revision": scene["revision"]}
