"""Immutable, receiver-owned examples from another loaded scene."""

from __future__ import annotations

import copy
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any
import uuid

from core import APIError, ASSET_RE, MAX_MODEL_BYTES, SceneStore, _now, _safe_json, _vector

REFERENCE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_SCENE_REFS = 4
MAX_CAPTURE_BYTES = 200 * 1024 * 1024
MAX_FEEDBACK_BYTES = 400 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_INTEGRITY_CACHE = 1024
MEDIA_URL = re.compile(r"^/media/[0-9a-f]{32}\.(?:png|jpg)$")


def _json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())


def _media(store: SceneStore, data: bytes) -> tuple[str, Path]:
    extension, _ = store._image_kind(data)
    path = store.media_dir / (uuid.uuid4().hex + "." + extension)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return "/media/" + path.name, path


def _file(root: Path, name: str) -> Path:
    path = root / name
    if root.resolve() != root or path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise APIError(409, f"scene reference file is missing or outside its archive: {name}")
    return path


def _resource(store: SceneStore, url: Any, kind: str) -> Path:
    pattern = ASSET_RE if kind == "assets" else MEDIA_URL
    if not isinstance(url, str) or not pattern.fullmatch(url):
        raise APIError(400, "scene reference needs a managed local asset URL")
    return _file(store.data_dir / kind, url.rsplit("/", 1)[-1])


def _read(path: Path, limit: int = MAX_RECORD_BYTES) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise APIError(400, "scene reference metadata exceeds the limit")
        value = json.loads(data)
    except (OSError, ValueError, UnicodeError) as exc:
        raise APIError(409, "scene reference archive is unreadable") from exc
    if not isinstance(value, dict):
        raise APIError(409, "scene reference archive must be an object")
    return value


def _pixels(store: SceneStore, data: bytes) -> None:
    from PIL import Image, UnidentifiedImageError
    import io
    store._image_kind(data)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise APIError(400, "scene reference image cannot be fully decoded") from exc


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while data := handle.read(1024 * 1024):
            result.update(data)
    return result.hexdigest()


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def public_reference(record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    prefix = "/p/" + result["receiver_project_id"]
    for obj in result["scene"]["objects"]:
        if obj.get("url"):
            obj["url"] = prefix + obj["url"]
    for asset in result["assets"]:
        asset["url"] = prefix + asset["url"]
    if result.get("source_reference_image"):
        result["source_reference_image"]["url"] = prefix + result["source_reference_image"]["url"]
    return result


class SceneReferences:
    def __init__(self, store: SceneStore):
        self.store = store
        self.root = store.data_dir / "scene_references"
        with store.lock:
            if not hasattr(store, "_scene_reference_lock"):
                store._scene_reference_lock = threading.RLock()
            if not hasattr(store, "_scene_reference_integrity_cache"):
                store._scene_reference_integrity_cache = OrderedDict()
                store._scene_reference_integrity_lock = threading.Lock()
            self.lock = store._scene_reference_lock
            self.integrity_cache = store._scene_reference_integrity_cache
            self.integrity_lock = store._scene_reference_integrity_lock

    def _verify_digest(self, path: Path, expected: Any, *, full_check: bool) -> None:
        if not isinstance(expected, str):
            raise APIError(409, "scene reference file digest is invalid")
        try:
            stamp = _stamp(path.stat())
            if not full_check:
                with self.integrity_lock:
                    if self.integrity_cache.get(path) == (stamp, expected):
                        self.integrity_cache.move_to_end(path)
                        return
            digest = _digest(path)
            if _stamp(path.stat()) != stamp or digest != expected:
                raise APIError(409, "scene reference file content changed")
        except OSError as exc:
            raise APIError(409, "scene reference file cannot be read") from exc
        # Cache only complete checks of stable files. Sending feedback always
        # rereads the bytes; repeated previews may reuse an unchanged inode.
        with self.integrity_lock:
            self.integrity_cache[path] = (stamp, digest)
            self.integrity_cache.move_to_end(path)
            while len(self.integrity_cache) > MAX_INTEGRITY_CACHE:
                self.integrity_cache.popitem(last=False)

    def owner(self, session_id: str | None = None) -> tuple[str, str]:
        with self.store.lock:
            workspace = self.store.state.get("workspace", {})
            project = workspace.get("project_id")
            session = session_id or workspace.get("session_id")
            if (not isinstance(project, str) or not REFERENCE_ID.fullmatch(project)
                or not isinstance(session, str) or not REFERENCE_ID.fullmatch(session)
                or workspace.get("session_id") != session or session not in self.store.state["sessions"]):
                raise APIError(409, "scene reference belongs to another receiver session")
            return project, session

    def load(self, reference_id: Any, *, session_id: str | None = None, full_check: bool = False) -> dict[str, Any]:
        if not isinstance(reference_id, str) or not REFERENCE_ID.fullmatch(reference_id):
            raise APIError(400, "scene reference ID must be a lowercase UUID hex value")
        owner, session = self.owner(session_id)
        directory = self.root / reference_id
        try:
            record = _read(_file(directory, "reference.json"))
        except APIError as exc:
            if not directory.exists():
                raise APIError(404, "scene reference not found in this receiver") from exc
            raise
        if (record.get("schema_version") != 1 or record.get("id") != reference_id
            or record.get("receiver_project_id") != owner or record.get("receiver_session_id") != session):
            raise APIError(404, "scene reference not found in this receiver session")
        if record.get("read_only") is not True or not isinstance(record.get("scene"), dict):
            raise APIError(409, "scene reference archive is invalid")
        expected = directory / "scene.json"
        if record.get("snapshot_json_path") != str(expected):
            raise APIError(409, "scene reference snapshot path is invalid")
        snapshot = _read(_file(directory, "scene.json"))
        if snapshot.get("scene") != record["scene"] or snapshot.get("source_scene_revision") != record.get("source_scene_revision"):
            raise APIError(409, "scene reference snapshot does not match its archive")
        assets = record.get("assets")
        if not isinstance(assets, list) or len(assets) > 500:
            raise APIError(409, "scene reference asset list is invalid")
        total = 0
        for asset in assets:
            if not isinstance(asset, dict):
                raise APIError(409, "scene reference asset metadata is invalid")
            path = _resource(self.store, asset.get("url"), "assets")
            if asset.get("path") != str(path) or type(asset.get("bytes")) is not int or path.stat().st_size != asset["bytes"]:
                raise APIError(409, "scene reference asset changed")
            self._verify_digest(path, asset.get("sha256"), full_check=full_check)
            total += asset["bytes"]
        if total > MAX_CAPTURE_BYTES:
            raise APIError(409, "scene reference asset budget exceeded")
        if record.get("source_reference_image"):
            image = record["source_reference_image"]
            path = _resource(self.store, image.get("url"), "media")
            if image.get("path") != str(path) or image.get("bytes") != path.stat().st_size:
                raise APIError(409, "scene reference image path is invalid")
            self._verify_digest(path, image.get("sha256"), full_check=full_check)
        return record

    def get(self, reference_id: Any) -> dict[str, Any]:
        return public_reference(self.load(reference_id))

    def capture(self, source_store: SceneStore, evidence: dict[str, Any]) -> dict[str, Any]:
        """Evidence was copied under the source lock; file copies never write it."""
        with self.lock:
            return self._capture(source_store, evidence)

    def _capture(self, source_store: SceneStore, evidence: dict[str, Any]) -> dict[str, Any]:
        owner, session = self.owner()
        if evidence["source_project_id"] == owner:
            raise APIError(400, "choose another scene as the reference")
        scene = {key: copy.deepcopy(evidence["scene"][key]) for key in ("revision", "name", "objects") if key in evidence["scene"]}
        scene["objects"] = source_store._validate_objects(scene.get("objects"))
        objects = []
        assets: dict[str, Path] = {}
        total = 0
        stamps = []
        for obj in scene["objects"]:
            # Never copy arbitrary object metadata, tool settings or workspace
            # state. The up axis is the only metadata needed to render a model.
            item = {key: copy.deepcopy(obj[key]) for key in ("id", "name", "type", "position", "size", "rotation", "color", "url") if key in obj}
            up = obj.get("metadata", {}).get("up_axis")
            if isinstance(up, str) and up.lower() in {"y", "z"}:
                item["metadata"] = {"up_axis": up.lower()}
            if obj.get("type") == "model" and obj["url"] not in assets:
                path = _resource(source_store, obj["url"], "assets")
                info = path.stat()
                total += info.st_size
                if total > MAX_CAPTURE_BYTES:
                    raise APIError(413, "source scene GLBs exceed the 200 MiB reference limit")
                if not 20 <= info.st_size <= MAX_MODEL_BYTES:
                    raise APIError(400, "GLB must be between 20 bytes and 100 MB")
                assets[obj["url"]] = path
                stamps.append((obj["url"], info.st_size, info.st_mtime_ns))
            objects.append(item)
        scene["objects"] = objects
        image = copy.deepcopy(evidence.get("source_reference_image"))
        image_path = None
        if image:
            image_path = _resource(source_store, image["url"], "media")
            info = image_path.stat()
            stamps.append((image["url"], info.st_size, info.st_mtime_ns))
        if not objects and image is None:
            raise APIError(400, "source scene has no geometry or reference image")
        signature = hashlib.sha256(json.dumps({"owner": owner, "session": session, **evidence,
                                               "scene": scene, "stamps": stamps}, sort_keys=True,
                                              ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.resolve() != self.root:
            raise APIError(409, "scene reference archive directory is unsafe")
        index_path = self.root / "index.json"
        index = _read(_file(self.root, "index.json")) if index_path.exists() else {}
        reference_id = index.get(signature)
        if isinstance(reference_id, str):
            try:
                old = self.load(reference_id, session_id=session)
                if old.get("capture_key") == signature:
                    return public_reference(old)
            except APIError:
                # A damaged old archive remains untouched; a new capture gets
                # its own files and ID instead of rewriting historical evidence.
                pass
        reference_id = uuid.uuid4().hex
        directory = self.root / reference_id
        directory.mkdir(mode=0o700)
        owned: list[Path] = []
        temporary_index = self.root / (".index-" + uuid.uuid4().hex + ".json")
        try:
            copied_assets = []
            remapped = {}
            for url, source in assets.items():
                source = _resource(source_store, url, "assets")
                # _copy_glb validates the complete destination container. A
                # second validation of the source adds no verified evidence.
                copied_url, digest = self.store._copy_glb(source)
                path = self.store.assets_dir / copied_url.rsplit("/", 1)[-1]
                owned.append(path)
                os.chmod(path, 0o400)
                remapped[url] = copied_url
                copied_assets.append({"url": copied_url, "path": str(path), "sha256": digest, "bytes": path.stat().st_size})
            if sum(asset["bytes"] for asset in copied_assets) > MAX_CAPTURE_BYTES:
                raise APIError(413, "copied scene GLBs exceed the 200 MiB reference limit")
            for obj in scene["objects"]:
                if obj.get("url"):
                    obj["url"] = remapped[obj["url"]]
            if image is not None:
                _, data = source_store._read_reference_path(str(_resource(source_store, image["url"], "media")))
                _pixels(self.store, data)
                if image.get("camera"):
                    camera = self.store._normalize_reference_camera(image["camera"])
                    if self.store._image_dimensions(data) != (camera["intrinsics"]["width"], camera["intrinsics"]["height"]):
                        raise APIError(400, "source scene camera dimensions do not match its GT image")
                    image["camera"] = camera
                image["url"], path = _media(self.store, data)
                owned.append(path)
                image["path"] = str(path)
                image.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                os.chmod(path, 0o400)
            record = {"schema_version": 1, "id": reference_id, "capture_key": signature,
                      "receiver_project_id": owner, "receiver_session_id": session,
                      "created_at": _now(), "read_only": True, "scene": scene,
                      "assets": copied_assets, "snapshot_json_path": str(directory / "scene.json"),
                      **{key: evidence[key] for key in ("name", "source_project_id", "source_session_id", "source_scene_revision", "source_project_dir")}}
            if evidence.get("source_blend_path"):
                record["source_blend_path"] = evidence["source_blend_path"]
            if image is not None:
                record["source_reference_image"] = image
            else:
                record["warnings"] = ["来源没有参考图；本引用只提供三维场景。"]
            self.owner(session)
            with self.store.lock:
                if self.store.state["sessions"][session]["status"] != "open":
                    raise APIError(409, "receiver session closed while capturing the reference")
            _json(directory / "scene.json", {key: record[key] for key in ("name", "source_project_id", "source_scene_revision", "scene", "assets")})
            _json(directory / "reference.json", record)
            index[signature] = reference_id
            _json(temporary_index, index)
            os.replace(temporary_index, index_path)
            return public_reference(record)
        except BaseException:
            for path in owned:
                path.unlink(missing_ok=True)
            shutil.rmtree(directory)
            raise
        finally:
            temporary_index.unlink(missing_ok=True)


def _camera(camera: Any, store: SceneStore, data: bytes) -> dict[str, Any]:
    from dynamic import number
    if not isinstance(camera, dict) or set(camera) - {"position", "target", "up", "fov", "aspect", "near", "far"}:
        raise APIError(400, "scene reference preview needs a perspective camera")
    result = {key: _vector(camera.get(key), "preview_camera." + key) for key in ("position", "target", "up")}
    if result["position"] == result["target"] or not any(result["up"]):
        raise APIError(400, "scene reference camera needs a distinct target and nonzero up")
    result["fov"] = number(camera.get("fov"), "preview_camera.fov", minimum=.001, maximum=179.999)
    result["aspect"] = number(camera.get("aspect"), "preview_camera.aspect", minimum=.0001, maximum=10000)
    width, height = store._image_dimensions(data)
    if abs(width - result["aspect"] * height) > 2:
        raise APIError(400, "scene reference preview camera aspect must match its image")
    for field in ("near", "far"):
        if field in camera:
            result[field] = number(camera[field], "preview_camera." + field, minimum=.000001, maximum=1_000_000)
    if "near" in result and "far" in result and result["far"] <= result["near"]:
        raise APIError(400, "scene reference camera far must exceed near")
    return result


def prepare_scene_refs(store: SceneStore, session_id: str, payload: dict[str, Any], note: str) -> list[dict[str, Any]]:
    from dynamic import number
    items = payload.get("scene_refs", [])
    if not isinstance(items, list) or len(items) > MAX_SCENE_REFS:
        raise APIError(400, "scene_refs must contain at most 4 archived references")
    archive = SceneReferences(store)
    result = []
    seen = set()
    total = 0
    cited = set(re.findall(r"\[\[scene:([0-9a-f]{32})\]\]", note))
    for item in items:
        if not isinstance(item, dict) or set(item) - {"id", "preview_data_url", "preview_camera", "time_sec"}:
            raise APIError(400, "scene_refs accepts only archived ID and preview evidence")
        reference_id = item.get("id")
        if not isinstance(reference_id, str) or not REFERENCE_ID.fullmatch(reference_id):
            raise APIError(400, "scene reference ID must be a lowercase UUID hex value")
        if reference_id in seen:
            raise APIError(400, "scene_refs must have unique IDs")
        record = archive.load(reference_id, session_id=session_id, full_check=True)
        if reference_id not in cited:
            raise APIError(400, "scene_refs contains a scene not cited in note")
        seen.add(reference_id)
        total += sum(asset["bytes"] for asset in record["assets"])
        if total > MAX_FEEDBACK_BYTES:
            raise APIError(413, "referenced GLBs exceed the 400 MiB feedback limit")
        preview = None
        camera = None
        if item.get("preview_data_url") is not None:
            preview = store._decode_image_data_url(item["preview_data_url"])
            _pixels(store, preview)
            camera = _camera(item.get("preview_camera"), store, preview)
        elif item.get("preview_camera") is not None:
            raise APIError(400, "scene reference camera requires preview pixels")
        result.append({"reference": record, "preview": preview, "preview_camera": camera,
                       **({"time_sec": number(item["time_sec"], "scene reference time_sec")} if "time_sec" in item else {})})
    return result


def publish_scene_refs(store: SceneStore, prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    owned = []
    try:
        for item in prepared:
            record = item["reference"]
            reference = {key: copy.deepcopy(record[key]) for key in ("id", "name", "source_project_id", "source_session_id", "source_scene_revision", "read_only", "snapshot_json_path", "assets", "source_project_dir")}
            reference["archive_path"] = str(store.data_dir / "scene_references" / record["id"] / "reference.json")
            for key in ("source_reference_image", "source_blend_path"):
                if record.get(key) is not None:
                    reference[key] = copy.deepcopy(record[key])
            if item["preview"] is not None:
                reference["preview_url"], path = _media(store, item["preview"])
                owned.append(path)
                reference["preview_path"] = str(path)
                reference["preview_camera"] = item["preview_camera"]
            if "time_sec" in item:
                reference["time_sec"] = item["time_sec"]
            result.append(reference)
    except BaseException:
        for path in owned:
            path.unlink(missing_ok=True)
        raise
    return result
