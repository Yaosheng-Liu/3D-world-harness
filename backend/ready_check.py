"""Read-only readiness checks using the same model and reference import gates."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
import re
import struct
from typing import Any
import warnings

from core import APIError, SceneStore
from gateway import WorkspaceGateway
from ready_import import ReadyInstance, _document, _scoped_file, discover_ready_instances


class _ReadOnlyReferences:
    """Only pure source reads/validation; publication never writes media or state."""

    _read_reference_path = SceneStore._read_reference_path
    _image_kind = staticmethod(SceneStore._image_kind)
    _image_dimensions = staticmethod(SceneStore._image_dimensions)
    _normalize_reference_camera = staticmethod(SceneStore._normalize_reference_camera)

    def _set_prepared_reference_views(self, _session: str, _controls: dict[str, Any],
                                      prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return prepared


def _message(exc: Exception) -> str:
    return exc.message if isinstance(exc, APIError) else str(exc)[:500]


def _decode_reference_pixels(data: bytes) -> None:
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if not 0 < width <= 32768 or not 0 < height <= 32768 or width * height > 50_000_000:
                    raise APIError(400, "reference image dimensions exceed the import limits")
                image.load()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise APIError(400, "reference image pixels cannot be fully decoded") from exc


def _check(status: str, code: str, message: str, *, file: Path | str | None = None,
           details: Any = None) -> dict[str, Any]:
    result = {"status": status, "code": code, "message": message}
    if file is not None:
        result["file"] = str(file)
    if details is not None:
        result["details"] = details
    return result


def _model_metrics(source: Path) -> dict[str, Any]:
    # The exact file has already passed SceneStore's complete GLB source gate.
    # Read only its bounded JSON chunk; animation ranges here are metadata, not
    # a Blender reopen, animation playback or reconstruction-quality claim.
    with source.open("rb") as handle:
        handle.seek(12)
        length, kind = struct.unpack("<I4s", handle.read(8))
        if kind != b"JSON" or not 0 < length <= 16 * 1024 * 1024:
            raise APIError(400, "GLB changed while checking its metadata")
        document = json.loads(handle.read(length))
    if not isinstance(document, dict):
        raise APIError(400, "GLB changed while checking its metadata")

    def items(name: str) -> list[Any]:
        value = document.get(name, [])
        return value if isinstance(value, list) else []

    result = {"glb_bytes": source.stat().st_size, "mesh_count": len(items("meshes")),
              "node_count": len(items("nodes")), "animation_count": len(items("animations"))}
    required = items("extensionsRequired")
    used = items("extensionsUsed")
    unavailable = [extension for extension in ("KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_texture_basisu")
                   if extension in required or (extension == "KHR_draco_mesh_compression" and extension in used)]
    if unavailable:
        result["viewer_missing_decoders"] = unavailable
    accessors = items("accessors")
    spans = []
    for animation in items("animations"):
        if not isinstance(animation, dict) or not isinstance(animation.get("samplers", []), list):
            continue
        for sampler in animation.get("samplers", []):
            index = sampler.get("input") if isinstance(sampler, dict) else None
            if type(index) is not int or not 0 <= index < len(accessors):
                continue
            accessor = accessors[index]
            if not isinstance(accessor, dict):
                continue
            minimum, maximum = accessor.get("min"), accessor.get("max")
            if (isinstance(minimum, list) and isinstance(maximum, list) and len(minimum) == len(maximum) == 1
                and all(type(value) in (int, float) and math.isfinite(value) for value in (*minimum, *maximum))
                and minimum[0] <= maximum[0]):
                spans.append((float(minimum[0]), float(maximum[0])))
    if spans:
        result["animation_time_span_sec"] = {"start": min(span[0] for span in spans),
                                             "end": max(span[1] for span in spans), "source": "accessor_metadata"}
    return result


def _prepare_references(instance: ReadyInstance) -> list[dict[str, Any]]:
    # Bypass __init__/ensure entirely: no thread, workspace, credentials, data
    # directory or queue is created. The real path/camera/time preparation runs
    # unchanged, including fully bounded ffmpeg decoding in its temporary dir.
    gateway = WorkspaceGateway.__new__(WorkspaceGateway)
    gateway.project_dir = instance.root
    gateway.store = _ReadOnlyReferences()
    gateway.ensure = lambda: {"session_id": "read-only-check"}
    payload = {"manifest_path": str(instance.manifest)}
    if instance.camera_manifest is not None:
        payload["camera_manifest_path"] = str(instance.camera_manifest)
    return gateway.set_reference_clip_paths(payload)


def _check_instance(instance: ReadyInstance) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"static_image_count": len(instance.reference_images), "view_count": 0,
                               "frame_count": 0, "camera_count": 0, "views": [], "editable_blend": False}
    try:
        source = _scoped_file(instance.root, str(instance.glb))
        SceneStore._validate_glb_source(str(source))
        metrics.update(_model_metrics(source))
        checks.append(_check("pass", "glb_valid", "GLB 容器、内嵌资源及导入限制检查通过。", file=source,
                             details={key: metrics[key] for key in ("glb_bytes", "mesh_count", "node_count", "animation_count")}))
        if metrics.get("viewer_missing_decoders"):
            checks.append(_check("error", "viewer_decoder_missing", "模型需要当前工作台未配置的压缩解码器，无法在视口显示。",
                                 file=source, details={"extensions": metrics["viewer_missing_decoders"]}))
        if metrics["mesh_count"] == 0:
            checks.append(_check("warning", "model_without_meshes", "模型未声明网格，导入后可能没有可见几何。", file=source))
    except (APIError, OSError, RuntimeError, ValueError, TypeError, struct.error) as exc:
        checks.append(_check("error", "glb_invalid", "模型无法导入：" + _message(exc), file=instance.glb))

    if instance.blend is not None:
        try:
            blend = _scoped_file(instance.root, str(instance.blend))
            with blend.open("rb") as handle:
                handle.read(1)
            metrics["editable_blend"] = True
            checks.append(_check("pass", "editable_source", "已提供可读取的 Blender 源文件；未执行 Blender 重开验证。", file=blend))
        except (APIError, OSError, RuntimeError, ValueError) as exc:
            checks.append(_check("error", "editable_source_invalid", "声明的 Blender 源文件不可读取：" + _message(exc), file=instance.blend))
    else:
        checks.append(_check("warning", "editable_source_missing", "未提供 Blender 源文件；GLB 仍可导入，无法据此确认可编辑源工程。"))

    if instance.reference_images:
        sizes = []
        failed_image = instance.reference_images[0]
        try:
            store = _ReadOnlyReferences()
            for image in instance.reference_images:
                failed_image = image
                source = _scoped_file(instance.root, str(image))
                _, data = store._read_reference_path(str(source))
                _decode_reference_pixels(data)
                sizes.append({"file": str(source), "bytes": len(data), "dimensions": list(store._image_dimensions(data))})
            checks.append(_check("pass", "reference_images", "全部静态参考图通过 PNG/JPEG 内容和大小检查。", details=sizes))
            checks.append(_check("warning", "static_camera_missing", "静态参考图未声明导入用相机；可手动比较，无法自动对齐这些图。"))
        except (APIError, OSError, RuntimeError, ValueError) as exc:
            checks.append(_check("error", "reference_images_invalid", f"静态参考图 {failed_image.name} 无法导入：" + _message(exc),
                                 file=failed_image))

    if instance.manifest is not None:
        try:
            prepared = _prepare_references(instance)
            for view in prepared:
                frames = view["frames"]
                for frame in frames:
                    try:
                        _decode_reference_pixels(frame["data"])
                    except APIError as exc:
                        raise APIError(400, f"{view['name']} frame {frame['frame_index']}: {exc.message}") from exc
                metrics["views"].append({"name": view["name"], "source_type": view["source_type"],
                                          "fps": view["fps"], "duration_sec": view["duration_sec"],
                                          "frame_count": len(frames), "camera_count": sum("camera" in frame for frame in frames),
                                          "image_bytes": sum(len(frame["data"]) for frame in frames)})
            metrics.update(view_count=len(prepared), frame_count=sum(view["frame_count"] for view in metrics["views"]),
                           camera_count=sum(view["camera_count"] for view in metrics["views"]))
            checks.append(_check("pass", "reference_sequence", "所有参考机位、帧图、帧率和时间戳通过实际导入检查。",
                                 file=instance.manifest, details=metrics["views"]))
            if metrics["camera_count"] < metrics["frame_count"]:
                checks.append(_check("warning", "camera_coverage", "部分参考帧未提供相机；可导入并手动比较，这些帧不能自动对齐。",
                                     details={"frames": metrics["frame_count"], "cameras": metrics["camera_count"]}))
            else:
                checks.append(_check("pass", "camera_coverage", "全部参考帧的相机参数与图片尺寸匹配。",
                                     details={"frames": metrics["frame_count"], "cameras": metrics["camera_count"]}))
        except (APIError, OSError, RuntimeError, ValueError, TypeError) as exc:
            checks.append(_check("error", "reference_sequence_invalid", "参考序列无法导入：" + _message(exc), file=instance.manifest))
    elif not instance.reference_images:
        checks.append(_check("warning", "reference_missing", "这是仅模型实例：可导入三维场景，但未附参考图或相机，不能自动对齐。"))
    failed = any(check["status"] == "error" for check in checks)
    warning = any(check["status"] == "warning" for check in checks)
    return {"name": instance.name, "path": str(instance.root), "format": instance.format,
            "status": "blocked" if failed else "warning" if warning else "ready", "can_import": not failed,
            "checks": checks, "metrics": metrics}


def check_ready_instance(instance: ReadyInstance) -> dict[str, Any]:
    """Validate one newly discovered source before allocating import data."""
    return {**_check_instance(instance), "validation_basis": "source"}


def check_saved_workspace(name: str, source: Path, format: str, data_dir: Path,
                          state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the retained scene/evidence without opening or changing a store.

    A repeated import restores edited managed evidence. Checking the original
    export instead would both reject good saved edits and miss damaged assets.
    """
    from dynamic import MAX_LOCAL_CLIP_BYTES, MAX_REFERENCE_VIEWS, prepare_clip, reference_views

    checks = [_check("pass", "preserved_scene", "复用已保存的场景和参考数据；原始导入文件不会覆盖当前编辑。")]
    metrics: dict[str, Any] = {"static_image_count": 0, "view_count": 0, "frame_count": 0,
                               "camera_count": 0, "views": [], "glb_bytes": 0, "mesh_count": 0,
                               "node_count": 0, "animation_count": 0, "model_count": 0}
    try:
        if data_dir.resolve(strict=True) != data_dir or not data_dir.is_dir():
            raise APIError(400, "saved workspace data directory changed")
        if state is None:
            state = _document(data_dir, data_dir / "state.json", limit=128 * 1024 * 1024)
        if not isinstance(state, dict) or not isinstance(state.get("scene"), dict):
            raise APIError(400, "saved workspace needs a scene")
        store = _ReadOnlyReferences()
        store.assets_dir = data_dir / "assets"
        store._validate_object = lambda obj: SceneStore._validate_object(store, obj)
        objects = SceneStore._validate_objects(store, state["scene"].get("objects"))
        for obj in objects:
            if obj["type"] != "model":
                continue
            asset = _scoped_file(data_dir / "assets", obj["url"].rsplit("/", 1)[-1])
            model = _check_instance(ReadyInstance(data_dir, obj["name"], asset, None,
                                                   "managed_scene", data_dir / "state.json"))
            checks.extend(check for check in model["checks"] if check["code"] in {
                "glb_valid", "glb_invalid", "viewer_decoder_missing", "model_without_meshes"})
            metrics["model_count"] += 1
            for field in ("glb_bytes", "mesh_count", "node_count", "animation_count"):
                metrics[field] += model["metrics"].get(field, 0)
        if not objects:
            checks.append(_check("warning", "scene_empty", "已保存的场景没有对象；保留此场景仍可继续编辑。"))
        workspace = state.get("workspace", {})
        if not isinstance(workspace, dict):
            raise APIError(400, "saved workspace metadata must be an object")
        sessions = state.get("sessions")
        session_id = workspace.get("session_id")
        if not isinstance(sessions, dict) or not isinstance(sessions.get(session_id), dict):
            raise APIError(400, "saved workspace session is missing")
        session = sessions[session_id]

        def media(entry: Any) -> bytes:
            if not isinstance(entry, dict) or not isinstance(entry.get("url"), str) or not re.fullmatch(
                r"/media/[0-9a-f]{32}\.(?:png|jpg)", entry["url"]
            ):
                raise APIError(400, "saved reference needs a managed media URL")
            try:
                path = _scoped_file(data_dir / "media", entry["url"].rsplit("/", 1)[-1])
                _, data = store._read_reference_path(str(path))
                _decode_reference_pixels(data)
                if entry.get("alignment_image_url") is not None:
                    if entry.get("camera") is None:
                        raise APIError(400, "saved alignment image requires a camera")
                    aligned = media({"url": entry["alignment_image_url"]})
                    if store._image_dimensions(aligned) != store._image_dimensions(data):
                        raise APIError(400, f"{entry['alignment_image_url']}: saved alignment image dimensions must match the reference")
            except (APIError, OSError, RuntimeError, ValueError) as exc:
                raise APIError(400, f"{entry['url']}: {_message(exc)}") from exc
            return data

        references = session.get("reference_images", [])
        if not isinstance(references, list) or len(references) > 8:
            raise APIError(400, "saved workspace may contain at most 8 reference images")
        metrics["static_image_count"] = len(references)
        static_cameras = 0
        for reference in references:
            data = media(reference)
            if reference.get("camera") is not None:
                camera = store._normalize_reference_camera(reference["camera"])
                if store._image_dimensions(data) != (camera["intrinsics"]["width"], camera["intrinsics"]["height"]):
                    raise APIError(400, "saved reference camera dimensions must match the image")
                static_cameras += 1
        if references:
            checks.append(_check("pass", "reference_images", "已保存的静态参考图通过内容和相机检查。"))
            if static_cameras < len(references):
                checks.append(_check("warning", "static_camera_missing", "部分已保存静态参考图没有相机；仍可手动比较。"))
        clip = session.get("reference_clip")
        if clip is not None:
            if not isinstance(clip, dict) or not isinstance(clip.get("views", []), list):
                raise APIError(400, "saved reference clip must be an object with a view list")
            views = reference_views(clip)
            if not 1 <= len(views) <= MAX_REFERENCE_VIEWS:
                raise APIError(400, "saved reference group must contain 1 to 8 views")
            for view in views:
                if not isinstance(view, dict) or not all(field in view for field in ("name", "fps", "duration_sec", "frames")):
                    raise APIError(400, "saved reference view is missing required metadata")
                if not isinstance(view["frames"], list) or not 1 <= len(view["frames"]) <= 600:
                    raise APIError(400, "saved reference view must contain 1 to 600 frames")
                local_frames = []
                for frame in view["frames"]:
                    if not isinstance(frame, dict) or "time_sec" not in frame:
                        raise APIError(400, "saved reference frame needs its timestamp")
                    local_frames.append({**{key: frame[key] for key in ("name", "time_sec", "camera") if key in frame},
                                         "data": media(frame)})
                prepared = prepare_clip(store, {key: view[key] for key in ("name", "fps", "duration_sec")},
                                        local_frames=local_frames, source_type=view.get("source_type", "sequence"),
                                        byte_limit=MAX_LOCAL_CLIP_BYTES, write_media=False)
                metrics["views"].append({"name": prepared["name"], "source_type": prepared["source_type"],
                                          "fps": prepared["fps"], "duration_sec": prepared["duration_sec"],
                                          "frame_count": len(prepared["frames"]),
                                          "camera_count": sum("camera" in frame for frame in prepared["frames"]),
                                          "image_bytes": sum(len(frame["data"]) for frame in prepared["frames"])})
            metrics.update(view_count=len(views), frame_count=sum(view["frame_count"] for view in metrics["views"]),
                           camera_count=sum(view["camera_count"] for view in metrics["views"]))
            checks.append(_check("pass", "reference_sequence", "已保存的全部参考帧、相机和时间戳通过检查。", details=metrics["views"]))
            if metrics["camera_count"] < metrics["frame_count"]:
                checks.append(_check("warning", "camera_coverage", "部分已保存参考帧没有相机；仍可手动比较。"))
        elif not references:
            checks.append(_check("warning", "reference_missing", "已保存场景未附参考图；仍可继续三维编辑，不能自动对齐。"))
    except (APIError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        checks.append(_check("error", "saved_workspace_invalid", "已保存场景检查失败：" + _message(exc), file=data_dir / "state.json"))
    failed = any(check["status"] == "error" for check in checks)
    return {"name": name, "path": str(source), "format": format, "validation_basis": "managed",
            "status": "blocked" if failed else "warning" if any(check["status"] == "warning" for check in checks) else "ready",
            "can_import": not failed, "checks": checks, "metrics": metrics}


def ready_check_report(discovery: Any, instances: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a shared discovery with source or preserved-data checks."""
    instances = list(instances)
    errors = list(discovery.errors)
    skipped = list(discovery.skipped)
    # An otherwise empty root with a broken catalog should explain its JSON
    # error. Unrelated valid manifests remain ignored and ready descendants are
    # never hidden by this diagnostic fallback.
    marker = discovery.path / "manifest.json"
    if not instances and not errors and marker.exists():
        try:
            _document(discovery.path, marker)
        except APIError as exc:
            errors.append({"path": str(marker), "error": "无法确认场景清单格式：" + exc.message})
    for error in errors:
        instances.append({"name": Path(error["path"]).name, "path": error["path"], "format": "unrecognized",
                          "status": "blocked", "can_import": False,
                          "checks": [_check("error", "discovery_error", error["error"], file=error["path"])], "metrics": {}})
    for instance in instances:
        for check in instance["checks"]:
            if check["status"] == "error" and check["code"] != "discovery_error":
                errors.append({"path": instance["path"], "error": check["message"], "code": check["code"]})
    counters = {status: sum(instance["status"] == status for instance in instances) for status in ("ready", "warning", "blocked")}
    counters.update(skipped=len(skipped), instances=len(instances), candidates=discovery.candidates,
                    directories_scanned=discovery.directories_scanned, entries_scanned=discovery.entries_scanned)
    can_import = any(instance["can_import"] for instance in instances)
    if can_import and (counters["blocked"] or discovery.truncated):
        status = "partial"
    elif can_import:
        status = "warning" if counters["warning"] else "ready"
    else:
        status = "blocked" if errors or discovery.truncated else "empty"
    return {"path": str(discovery.path), "status": status, "can_import": can_import, "counters": counters,
            "instances": instances, "skipped": skipped, "errors": errors, "truncated": discovery.truncated}


def check_ready_folder(path: Any) -> dict[str, Any]:
    """Check discoverable imports without changing sources or workspace state."""
    discovery = discover_ready_instances(path)
    return ready_check_report(discovery, [check_ready_instance(instance) for instance in discovery.instances])
