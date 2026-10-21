"""Bounded reference clips and immutable, timed visual evidence."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from core import APIError, MAX_REFERENCE_BYTES, _safe_json

MAX_CLIP_FRAMES = 1200
MAX_REFERENCE_VIEWS = 8
MAX_CLIP_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MULTIVIEW_MANIFEST_BYTES = MAX_REFERENCE_VIEWS * MAX_CLIP_MANIFEST_BYTES
MAX_BROWSER_CLIP_BYTES = 40 * 1024 * 1024
MAX_LOCAL_CLIP_BYTES = 250 * 1024 * 1024
MAX_DYNAMIC_FRAMES = 8
IMAGE_FIELDS = ("reference_original", "reference_annotated", "scene_original", "scene_annotated")


def reference_views(clip: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Expose the original clip as the first view without duplicating its frames."""
    if not clip:
        return []
    return [{key: value for key, value in clip.items() if key != "views"}, *clip.get("views", [])]


def reference_frames(clip: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [frame for view in reference_views(clip) for frame in view.get("frames", [])]


def shared_duration(clip: dict[str, Any]) -> float:
    return max(view["duration_sec"] for view in reference_views(clip))


def reference_clip_metadata(clip: dict[str, Any]) -> dict[str, Any]:
    result = {key: copy.deepcopy(value) for key, value in clip.items() if key not in {"frames", "views"}}
    if clip.get("views"):
        result["views"] = [{key: copy.deepcopy(value) for key, value in view.items() if key != "frames"} for view in clip["views"]]
    result["shared_duration_sec"] = shared_duration(clip)
    return result


def number(value: Any, label: str, *, minimum: float = 0, maximum: float = 86400) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise APIError(400, f"{label} must be a finite number between {minimum} and {maximum}")
    return float(value)


def fps_value(value: Any = None) -> float:
    return number(30 if value is None else value, "fps", minimum=0.1, maximum=120)


def clip_name(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 180 or any(ord(char) < 32 for char in value):
        raise APIError(400, "clip name must be short text")
    return value


def decode_video(data_url: Any) -> bytes:
    if not isinstance(data_url, str) or len(data_url) > 4 * ((MAX_BROWSER_CLIP_BYTES + 2) // 3) + 100:
        raise APIError(400, "video must be a data URL up to 40 MB")
    match = re.fullmatch(r"data:(?:video/[A-Za-z0-9.+-]+|application/octet-stream);base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise APIError(400, "video must be a base64 video data URL")
    try:
        data = base64.b64decode(match.group(1), validate=True)
    except binascii.Error as exc:
        raise APIError(400, "invalid video encoding") from exc
    if not 0 < len(data) <= MAX_BROWSER_CLIP_BYTES:
        raise APIError(400, "video exceeds 40 MB")
    return data


def sample_video(source: Path, fps: float) -> tuple[list[dict[str, Any]], float]:
    """Decode a whole bounded clip; reject overflow instead of silently truncating."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise APIError(400, "video import requires ffmpeg and ffprobe; image sequences work without them")
    if not 0 < source.stat().st_size <= MAX_LOCAL_CLIP_BYTES:
        raise APIError(400, "local video must be a file up to 250 MB")
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration:format=duration,format_name", "-of", "json", str(source)], capture_output=True, check=True, timeout=15)
        document = json.loads(probe.stdout)
        streams = document.get("streams", [])
        if not streams or not set(document.get("format", {}).get("format_name", "").split(",")) & {"mov", "mp4", "matroska", "webm", "avi", "mpeg", "mpegts", "ogg"}:
            raise APIError(400, "video format is unsupported or has no video stream")
        stream = streams[0]
        if not 0 < int(stream.get("width", 0)) <= 20000 or not 0 < int(stream.get("height", 0)) <= 20000:
            raise APIError(400, "video dimensions are unsupported")
        duration = number(float(stream.get("duration") or document.get("format", {}).get("duration")), "video duration", minimum=0.001)
        if math.ceil(duration * fps - 1e-6) > MAX_CLIP_FRAMES:
            raise APIError(400, f"video would exceed {MAX_CLIP_FRAMES} sampled frames; choose a lower fps or a shorter clip")
        with tempfile.TemporaryDirectory(prefix="scene-feedback-video-") as temporary:
            output = Path(temporary)
            # The extra frame exposes inaccurate duration metadata without unbounded output.
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", f"fps={fps},scale=w='min(1600,iw)':h=-2", "-frames:v", str(MAX_CLIP_FRAMES + 1), "-q:v", "3", str(output / "%06d.jpg")], capture_output=True, check=True, timeout=90)
            files = sorted(output.glob("*.jpg"))
            if not files or len(files) > MAX_CLIP_FRAMES:
                raise APIError(400, f"video must decode to 1–{MAX_CLIP_FRAMES} sampled frames")
            if sum(path.stat().st_size for path in files) > MAX_LOCAL_CLIP_BYTES:
                raise APIError(400, "sampled video frames exceed 250 MB")
            frames = [{"name": path.name, "time_sec": index / fps, "data": path.read_bytes()} for index, path in enumerate(files)]
            if frames[-1]["time_sec"] >= duration + 1 / fps:
                raise APIError(400, "decoded video timestamps exceed its reported duration")
            return frames, duration
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(400, "video could not be fully decoded within the import limits") from exc


def prepare_clip(store: Any, payload: Any, *, local_frames: list[dict[str, Any]] | None = None,
                 source_type: str = "sequence", byte_limit: int = MAX_BROWSER_CLIP_BYTES,
                 write_media: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise APIError(400, "clip must be an object")
    fps = fps_value(payload.get("fps"))
    name = clip_name(payload.get("name", "Reference clip"))
    frames = local_frames if local_frames is not None else payload.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_CLIP_FRAMES:
        raise APIError(400, f"clip frames must contain 1–{MAX_CLIP_FRAMES} images")
    prepared = []
    total_bytes = 0
    previous_time = -1.0
    for index, item in enumerate(frames):
        if not isinstance(item, dict):
            raise APIError(400, "clip frame must be an object")
        time_sec = number(item.get("time_sec", index / fps), "frame time_sec")
        if time_sec <= previous_time:
            raise APIError(400, "clip frame timestamps must be strictly increasing")
        previous_time = time_sec
        data = item.get("data") if local_frames is not None else store._decode_image_data_url(item.get("data_url"), max_bytes=MAX_REFERENCE_BYTES)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_REFERENCE_BYTES:
            raise APIError(400, "clip frame image exceeds 25 MB")
        store._image_kind(data)
        total_bytes += len(data)
        if total_bytes > byte_limit:
            raise APIError(400, f"clip frames exceed {byte_limit // (1024 * 1024)} MB")
        frame = {"id": uuid.uuid4().hex, "name": clip_name(item.get("name", f"Frame {index + 1}")),
                 "time_sec": time_sec, "frame_index": index}
        if item.get("camera") is not None:
            camera = store._normalize_reference_camera(item["camera"])
            if store._image_dimensions(data) != (camera["intrinsics"]["width"], camera["intrinsics"]["height"]):
                raise APIError(400, "clip camera dimensions must match the frame image")
            frame["camera"] = camera
        prepared.append((frame, data))
    duration = number(payload.get("duration_sec", prepared[-1][0]["time_sec"] + 1 / fps), "duration_sec", minimum=0.001)
    if prepared[-1][0]["time_sec"] > duration:
        raise APIError(400, "clip duration must include its last frame")
    # Write only after the entire import passes validation.
    result = {"clip_id": uuid.uuid4().hex, "name": name, "source_type": source_type, "fps": fps, "duration_sec": duration,
              "frames": [{**frame, **({"url": store._write_media(data)} if write_media else {"data": data})} for frame, data in prepared]}
    if source_type == "video":
        result["sampled"] = True
    return result


def publish_prepared_clip(store: Any, clip: dict[str, Any]) -> dict[str, Any]:
    result = {key: copy.deepcopy(value) for key, value in clip.items() if key != "frames"}
    result["frames"] = [{**{key: value for key, value in frame.items() if key != "data"}, "url": store._write_media(frame["data"])} for frame in clip["frames"]]
    return result


def prepare_dynamic_feedback(store: Any, session: dict[str, Any], payload: dict[str, Any],
                             revision: int, object_ids: set[str], model_ids: set[str], *, require_oldest: bool = True) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    timeline = payload.get("timeline")
    frames = payload.get("dynamic_frames", [])
    if timeline is None:
        if frames:
            raise APIError(400, "dynamic_frames require timeline metadata")
        return None, []
    if not isinstance(timeline, dict):
        raise APIError(400, "timeline must be an object")
    clip = session.get("reference_clip")
    clip_id = timeline.get("clip_id")
    if clip_id is not None and (not clip or clip_id != clip["clip_id"]):
        raise APIError(400, "timeline clip_id does not match the current reference clip")
    if clip and clip_id is None:
        raise APIError(400, "timeline clip_id must identify the current reference clip")
    duration = number(timeline.get("duration_sec"), "timeline duration_sec", minimum=0.001)
    fps = fps_value(timeline.get("fps"))
    views = {view["clip_id"]: view for view in reference_views(clip)}
    timeline_view_id = timeline.get("view_id", clip_id)
    if timeline_view_id is not None and (not isinstance(timeline_view_id, str) or timeline_view_id not in views):
        raise APIError(400, "timeline view_id does not match a current reference view")
    if clip and (abs(duration - shared_duration(clip)) > 1e-4 or abs(fps - clip["fps"]) > 1e-4):
        raise APIError(400, "timeline duration/fps must match the current reference clip")
    time_sec = number(timeline.get("time_sec"), "timeline time_sec", maximum=duration)
    normalized_timeline = {"clip_id": clip_id, "time_sec": time_sec, "duration_sec": duration, "fps": fps}
    # Current clients describe modification times in the note. Keep timestamps
    # as evidence metadata, while preserving explicitly submitted legacy scope.
    if "scope" in timeline:
        scope = timeline["scope"]
        if not isinstance(scope, dict) or scope.get("kind") not in {"frame", "range", "clip"}:
            raise APIError(400, "timeline scope must be frame, range or clip")
        normalized_scope = {"kind": scope["kind"]}
        if scope["kind"] == "range":
            start = number(scope.get("start_sec"), "scope start_sec", maximum=duration)
            end = number(scope.get("end_sec"), "scope end_sec", maximum=duration)
            if start >= end:
                raise APIError(400, "scope range start_sec must precede end_sec")
            normalized_scope.update(start_sec=start, end_sec=end)
        elif "start_sec" in scope or "end_sec" in scope:
            raise APIError(400, "only range scope accepts start_sec/end_sec")
        normalized_timeline["scope"] = normalized_scope
    if timeline_view_id is not None:
        normalized_timeline.update(view_id=timeline_view_id, view_name=views[timeline_view_id]["name"])
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_DYNAMIC_FRAMES:
        raise APIError(400, "dynamic_frames must contain 1 to 8 frozen evidence frames")
    static_references = {reference["id"]: reference for reference in session.get("reference_images", [])}
    prepared = []
    seen = set()
    for item in frames:
        if not isinstance(item, dict):
            raise APIError(400, "dynamic frame must be an object")
        frame_id = item.get("id")
        if not isinstance(frame_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", frame_id) or frame_id in seen:
            raise APIError(400, "dynamic frame ids must be short, URL-safe and unique")
        seen.add(frame_id)
        frame_time = number(item.get("time_sec"), "dynamic frame time_sec", maximum=duration)
        frame_revision = item.get("scene_revision")
        if type(frame_revision) is not int or not revision <= frame_revision <= store.state["scene"]["revision"]:
            raise APIError(409, "dynamic frame scene_revision must be between the submitted and current scene revisions",
                           detail={"code": "feedback_revision_conflict", "current_scene_revision": store.state["scene"]["revision"]})
        if frame_revision != store.state["scene"]["revision"] and payload.get("confirm_stale") is not True:
            raise APIError(409, "scene revision changed; confirm stale dynamic evidence before submitting",
                           detail={"code": "feedback_revision_conflict", "current_scene_revision": store.state["scene"]["revision"]})
        reference_id = item.get("reference_frame_id")
        static_reference_id = item.get("static_reference_id")
        view_id = item.get("view_id", clip_id if reference_id is not None else None)
        if view_id is not None and (not isinstance(view_id, str) or view_id not in views):
            raise APIError(400, "dynamic frame view_id is unknown")
        view = views.get(view_id)
        references = {reference["id"]: reference for reference in view["frames"]} if view else {}
        # The authoritative ordinal belongs to this view's imported sequence.
        reference_indices = {reference["id"]: index for index, reference in enumerate(view["frames"])} if view else {}
        if static_reference_id is not None and (not isinstance(static_reference_id, str) or static_reference_id not in static_references):
            raise APIError(400, "dynamic frame static_reference_id is unknown")
        if reference_id is not None and static_reference_id is not None:
            raise APIError(400, "dynamic frame must choose a clip frame or a static reference")
        if reference_id is not None and (not isinstance(reference_id, str) or reference_id not in references):
            raise APIError(400, "dynamic frame reference_frame_id is unknown in its reference view")
        if reference_id is not None:
            nearest = min(references.values(), key=lambda reference: abs(reference["time_sec"] - frame_time))
            if abs(references[reference_id]["time_sec"] - frame_time) > abs(nearest["time_sec"] - frame_time) + 1e-5:
                raise APIError(400, "dynamic frame reference must be the nearest timestamp to its scene view")
        elif clip and static_reference_id is None:
            raise APIError(400, "dynamic frame must identify its reference clip frame")
        camera = item.get("camera")
        if not isinstance(camera, dict):
            raise APIError(400, "dynamic frame camera must be a small object")
        _safe_json(camera)
        if len(json.dumps(camera)) > 16000:
            raise APIError(400, "dynamic frame camera is too large")
        selected = item.get("selected_object_ids", [])
        if not isinstance(selected, list) or len(selected) > 100 or any(not isinstance(value, str) or value not in object_ids for value in selected) or len(selected) != len(set(selected)):
            raise APIError(400, "dynamic frame selected_object_ids must be unique scene objects")
        nodes = item.get("selected_scene_nodes", [])
        if not isinstance(nodes, list) or len(nodes) > 64:
            raise APIError(400, "dynamic frame selected_scene_nodes must contain at most 64 nodes")
        nodes = [store._scene_node(node, model_ids) for node in nodes]
        if any(node["parent_object_id"] not in selected for node in nodes):
            raise APIError(400, "dynamic frame selected node parent must be selected")
        node_keys = {(node["parent_object_id"], tuple(node["node_path"])) for node in nodes}
        if len(node_keys) != len(nodes):
            raise APIError(400, "dynamic frame selected scene nodes must be unique")
        frame = {"id": frame_id, "time_sec": frame_time, "scene_revision": frame_revision, "reference_frame_id": reference_id,
                 "camera": copy.deepcopy(camera), "selected_object_ids": list(selected), "selected_scene_nodes": nodes}
        if reference_id is not None:
            frame["frame_index"] = reference_indices[reference_id]
            frame.update(view_id=view_id, view_name=view["name"], reference_time_sec=references[reference_id]["time_sec"])
        elif view_id is not None:
            frame.update(view_id=view_id, view_name=view["name"])
        if static_reference_id is not None:
            frame["static_reference_id"] = static_reference_id
        animations = item.get("animation_clips", [])
        if not isinstance(animations, list) or len(animations) > 100:
            raise APIError(400, "animation_clips must contain at most 100 selections")
        normalized_animations = []
        seen_animations = set()
        for animation in animations:
            if not isinstance(animation, dict) or set(animation) - {"object_id", "name", "index"} or animation.get("object_id") not in model_ids:
                raise APIError(400, "animation clip must identify a model object")
            name = animation.get("name")
            if not isinstance(name, str) or len(name) > 160 or any(ord(char) < 32 for char in name):
                raise APIError(400, "animation clip name must be short text")
            normalized = {"object_id": animation["object_id"], "name": name}
            index = animation.get("index")
            if index is not None:
                if type(index) is not int or not 0 <= index <= 65535:
                    raise APIError(400, "animation clip index must be a nonnegative integer")
                normalized["index"] = index
            key = (animation["object_id"], name, index)
            if key in seen_animations:
                raise APIError(400, "animation clip selections must be unique")
            seen_animations.add(key)
            normalized_animations.append(normalized)
        if normalized_animations:
            frame["animation_clips"] = normalized_animations
        images = {}
        for field in IMAGE_FIELDS:
            data_url = item.get(field + "_data_url")
            if data_url is not None:
                images[field] = store._decode_image_data_url(data_url)
        if "scene_original" not in images:
            raise APIError(400, "dynamic frame needs a clean scene_original_data_url")
        if reference_id is not None or static_reference_id is not None:
            reference = references[reference_id] if reference_id is not None else static_references[static_reference_id]
            # Reference originals are always the immutable imported source pixels.
            if "reference_original" in images:
                del images["reference_original"]
            frame["reference_original_url"] = reference["url"]
            if reference.get("camera"):
                frame["reference_camera"] = copy.deepcopy(reference["camera"])
        elif "reference_original" in images:
            raise APIError(400, "reference original image requires a reference clip")
        from comparison import prepare_comparison
        comparison_fields, comparison_images = prepare_comparison(store, session, item)
        if comparison_fields and (reference_id or static_reference_id) and comparison_fields['comparison']['reference_id'] != (reference_id or static_reference_id):
            raise APIError(400, "comparison reference must match its saved evidence frame")
        frame.update(comparison_fields)
        images.update(comparison_images)
        prepared.append({"frame": frame, "images": images})
    if require_oldest and min(item["frame"]["scene_revision"] for item in prepared) != revision:
        raise APIError(409, "submitted scene_revision must be the oldest saved dynamic evidence revision",
                       detail={"code": "feedback_revision_conflict", "current_scene_revision": store.state["scene"]["revision"]})
    return normalized_timeline, prepared


def validate_timed_annotations(annotations: list[dict[str, Any]], timeline: dict[str, Any] | None,
                               frames: list[dict[str, Any]]) -> None:
    evidence = {item["frame"]["id"]: item["frame"] for item in frames}
    for annotation in annotations:
        # Client indices are display hints; only a validated clip evidence frame
        # can supply the authoritative ordinal in submitted feedback.
        annotation.pop("frame_index", None)
        timed = any(key in annotation for key in ("frame_id", "time_sec", "clip_id", "view_id"))
        if not timed:
            continue
        frame = evidence.get(annotation.get("frame_id"))
        if timeline is None or frame is None:
            raise APIError(400, "timed annotation must identify a saved dynamic evidence frame")
        if annotation.get("clip_id") != timeline["clip_id"] or type(annotation.get("scene_revision")) is not int or annotation["scene_revision"] != frame["scene_revision"] or type(annotation.get("time_sec")) not in (int, float) or abs(annotation["time_sec"] - frame["time_sec"]) > 1e-5:
            raise APIError(400, "annotation timestamp, clip and revision must match its evidence frame")
        if annotation.get("pane") == "reference" and annotation.get("reference_image_id") != (frame["reference_frame_id"] or frame.get("static_reference_id")):
            raise APIError(400, "timed annotation reference must match its evidence frame")
        view_id = frame.get("view_id")
        if annotation.get("view_id") != view_id and not (annotation.get("view_id") is None and view_id == timeline["clip_id"]):
            raise APIError(400, "annotation view_id must match its evidence frame")
        for key in ("view_name", "reference_time_sec"):
            annotation.pop(key, None)
        if view_id is not None:
            annotation["view_id"] = view_id
            annotation["view_name"] = frame["view_name"]
            if "reference_time_sec" in frame:
                annotation["reference_time_sec"] = frame["reference_time_sec"]
        if "frame_index" in frame:
            annotation["frame_index"] = frame["frame_index"]


def prepare_scene_snapshots(store: Any, session: dict[str, Any], payload: dict[str, Any],
                            revision: int, object_ids: set[str], model_ids: set[str]) -> list[dict[str, Any]]:
    """Validate static camera views with the same image/selection limits as frames.

    The synthetic timeline is local to validation; static views never acquire
    timestamps or timeline semantics in stored feedback or model attachments.
    """
    snapshots = payload.get("scene_snapshots", [])
    if not isinstance(snapshots, list) or len(snapshots) > MAX_DYNAMIC_FRAMES:
        raise APIError(400, "scene_snapshots must contain at most 8 camera views")
    if not snapshots:
        return []
    frames = []
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            raise APIError(400, "scene snapshot must be an object")
        name = snapshot.get("name", f"截图 {index + 1}")
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise APIError(400, "scene snapshot name must be short text")
        if any(key in snapshot for key in ("time_sec", "reference_frame_id", "view_id", "animation_clips")):
            raise APIError(400, "static scene snapshots cannot contain timeline fields")
        frames.append({**snapshot, "time_sec": 0})
    _, prepared = prepare_dynamic_feedback(store, {**session, "reference_clip": None}, {
        **payload, "dynamic_frames": frames,
        "timeline": {"clip_id": None, "time_sec": 0, "duration_sec": 1, "fps": 1, "scope": {"kind": "frame"}},
    }, revision, object_ids, model_ids, require_oldest=False)
    for index, item in enumerate(prepared):
        item["frame"].pop("time_sec", None)
        item["frame"].pop("reference_frame_id", None)
        item["frame"]["name"] = snapshots[index].get("name", f"截图 {index + 1}").strip()
    return prepared


def validate_snapshot_annotations(annotations: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> None:
    evidence = {item["frame"]["id"]: item["frame"] for item in snapshots}
    for annotation in annotations:
        if annotation.get("pane") != "scene" or annotation.get("frame_id"):
            continue
        snapshot = evidence.get(annotation.get("snapshot_id"))
        if snapshot is None:
            raise APIError(400, "scene annotation must identify a saved scene snapshot")
        if annotation.get("scene_revision") != snapshot["scene_revision"] or annotation.get("camera") != snapshot["camera"]:
            raise APIError(400, "scene annotation camera and revision must match its snapshot")
