"""Official MCP SDK stdio tools for the local 3D scene feedback harness."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import logging
import os
import re
import uuid
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from server import DEFAULT_DATA_DIR
from feedback_summary import image_caption, model_input_plan, feedback_result_manifest


PORT = int(os.environ.get("SCENE_FEEDBACK_PORT", "18765"))
DATA_DIR = Path(os.environ.get("SCENE_FEEDBACK_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser().resolve()
PROJECT_DIR = Path(os.environ.get("SCENE_FEEDBACK_PROJECT_DIR", str(Path(__file__).resolve().parent.parent))).expanduser().resolve()
BASE_URL = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
IMAGE_URL_RE = re.compile(r"^/(media|screenshots)/([0-9a-f]{32}\.(?:png|jpg))$")


def _http(method: str, path: str, payload: dict[str, Any] | None = None, *, private: bool = False, timeout: float = 10) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        headers["X-Scene-Harness-Key"] = (DATA_DIR / "control_token").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        if private:
            raise RuntimeError(f"Workspace Gateway control key is missing from {DATA_DIR}") from None
    request = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                detail = json.load(exc)["error"]
            except Exception:
                detail = exc.reason
        raise ValueError(f"HTTP {exc.code}: {detail}") from exc


def ensure_http_server() -> None:
    """Require the project Gateway started by the local workbench process."""
    try:
        health = _http("GET", "/api/health", timeout=2)
    except (ValueError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RuntimeError(f"Workspace Gateway unavailable at {BASE_URL}; start backend/server.py") from exc
    if health.get("service") != "scene-feedback-harness" or not health.get("workspace_gateway"):
        raise RuntimeError(f"port {PORT} is not running the current Workspace Gateway")
    if health.get("data_dir") != str(DATA_DIR) or health.get("project_dir") != str(PROJECT_DIR):
        raise RuntimeError(f"Workspace Gateway at {BASE_URL} belongs to another project or data directory; check SCENE_FEEDBACK_* settings")


def _image_path(url: str, data_dir: Path | None = None) -> Path:
    match = IMAGE_URL_RE.fullmatch(url)
    if match is None:
        raise ValueError("feedback contains an invalid image URL")
    directory = (data_dir or DATA_DIR) / match.group(1)
    return directory / match.group(2)


def _feedback_with_local_paths(result: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    for item in result.get("items", []):
        for reference in item.get("reference_images", []):
            reference["path"] = str(_image_path(reference["url"], data_dir))
        for reference in item.get("reference_annotated_images", []):
            reference["path"] = str(_image_path(reference["url"], data_dir))
        for crop in item.get("crops", []):
            crop["path"] = str(_image_path(crop["url"], data_dir))
        for image in item.get("image_refs", []):
            for name in ("original", "display_original", "annotated"):
                if image.get(name + "_url"):
                    image[name + "_path"] = str(_image_path(image[name + "_url"], data_dir))
        for pose in item.get("human_pose", []):
            for name in ("reference_original", "pose_overlay"):
                pose[name + "_path"] = str(_image_path(pose[name + "_url"], data_dir))
        for pose in item.get("human_pose_edits", []):
            for name in ("reference_original", "pose_overlay"):
                pose[name + "_path"] = str(_image_path(pose[name + "_url"], data_dir))
            feedback_id, edit_id = item.get("feedback_id"), pose.get("id")
            if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in (feedback_id, edit_id)):
                raise ValueError("feedback contains an invalid pose correction ID")
            pose["corrections_path"] = str((data_dir or DATA_DIR) / "human_pose" / "corrections" / feedback_id / (edit_id + ".json"))
        for name in ("scene_original", "scene_annotated", "screenshot", "comparison_reference_original", "comparison_reference", "scene_comparison"):
            url = item.get(f"{name}_url")
            if url:
                item[f"{name}_path"] = str(_image_path(url, data_dir))
        for reference in item.get("scene_refs", []):
            if reference.get("preview_url"):
                reference["preview_path"] = str(_image_path(reference["preview_url"], data_dir))
            original = reference.get("source_reference_image")
            if original and original.get("url"):
                original["path"] = str(_image_path(original["url"], data_dir))
        for frame in [*item.get("dynamic_frames", []), *item.get("scene_snapshots", [])]:
            for name in ("reference_original", "reference_annotated", "scene_original", "scene_annotated", "comparison_reference_original", "comparison_reference", "scene_comparison"):
                if frame.get(name + "_url"):
                    frame[name + "_path"] = str(_image_path(frame[name + "_url"], data_dir))
    return result


def _preview_image(path: Path) -> ImageContent:
    """Send a bounded MCP preview while retaining the original file on disk."""
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        opened.load()
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85, optimize=True)
    return ImageContent(type="image", data=base64.b64encode(output.getvalue()).decode("ascii"), mime_type="image/jpeg")


def _visual_tool_result(result: dict[str, Any], data_dir: Path | None = None,
                        *, include_details: bool = False) -> CallToolResult:
    directory = data_dir or DATA_DIR
    plans = [model_input_plan(item, directory) for item in result.get("items", [])]
    summary = feedback_result_manifest(result, directory, plans=plans)
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(summary, ensure_ascii=False, separators=(",", ":"))),
        TextContent(type="text", text="User marks are instructions; overlay ghosting is not geometry. Times are clip-relative capture times; modification times follow the prompt. Prompt frame numbers start at 1, frame_index at 0. Read details on demand; publish the updated GLB with workspace_publish_scene."),
    ]
    for item, plan in zip(result.get("items", []), plans):
        if len(plans) > 1:
            content.append(TextContent(type="text", text="Feedback " + item["feedback_id"]))
        if item.get("human_pose"):
            content.append(TextContent(type="text", text="Respect evidence_kind/keypoint_profile; projected_3d is not an observed 2D measurement. Cross-view identity requires evidence."))
        if item.get("human_pose_edits"):
            content.append(TextContent(type="text", text="Use $capsule-human-tracking apply-corrections with corrections_path. Only visible manual points are visible measurements; unchanged points retain parent_evidence_kind."))
        if plan["manifest"].get("scene_refs"):
            content.append(TextContent(type="text", text="Similar-scene references are read-only examples. Read snapshot_json_path and glb_paths on demand; edit only the current reconstruction, never its source scene."))
        for image in plan["images"]:
            content.append(TextContent(type="text", text=image_caption(image)))
            content.append(_preview_image(Path(image["path"])))
    structured = _feedback_with_local_paths(copy.deepcopy(result), directory) if include_details else summary
    return CallToolResult(content=content, structured_content=structured)


mcp = MCPServer(
    "scene-feedback-harness",
    instructions=(
        "For this configured project's 3D reconstruction task, if the user says '进入人工调试模式' or '进入人工参与调试模式', "
        "call request_visual_feedback with project-local reference images and current GLB if available. "
        "In external mode without a bound desktop task, wait for the annotated images, scene screenshot, "
        "selected object, and note to return as the tool result. With a bound desktop task, the "
        "workbench sends the submitted visual feedback as a new message to that same task. "
        "Continue editing there and publish the updated GLB with workspace_publish_scene."
    ),
)


def _external_state() -> dict[str, Any]:
    ensure_http_server()
    state = _http("GET", "/api/workspace/state")
    if state.get("delivery_mode") != "external":
        raise ValueError("this workbench uses its own Codex App Server task; start it with --external-review for an existing Codex task")
    return state


def _browser_url(state: dict[str, Any]) -> str:
    """Prefer the server's accessible browser link over the local API address."""
    return state.get("browser_url") or f"{BASE_URL}/?{urlencode({'session_id': state['session_id']})}"


async def _wait_for_external_feedback(session_id: str, timeout_sec: int, cursor: int, browser_url: str) -> dict[str, Any]:
    if type(timeout_sec) is not int or not 1 <= timeout_sec <= 3600:
        raise ValueError("timeout_sec must be between 1 and 3600")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a nonnegative integer")
    deadline = asyncio.get_running_loop().time() + timeout_sec
    query = urlencode({"session_id": session_id, "cursor": cursor})
    while True:
        result = await asyncio.to_thread(_http, "GET", f"/api/workspace/external/feedback?{query}", private=True)
        if result["items"] or result.get("status") != "open":
            result["url"] = browser_url
            return result
        if asyncio.get_running_loop().time() >= deadline:
            return {"session_id": session_id, "status": "timeout", "items": [], "next_cursor": cursor, "url": browser_url, "message": "The workbench remains open. Call wait_visual_feedback later with this session_id and next_cursor."}
        await asyncio.sleep(0.35)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True))
def workspace_open(open_browser: bool = True) -> dict[str, Any]:
    """Open or locate this project's persistent visual reconstruction workbench."""
    ensure_http_server()
    result = _http("GET", "/api/workspace/state")
    if result.get("delivery_mode") == "external":
        _http("POST", "/api/workspace/external/request", {"session_id": result["session_id"]}, private=True)
        result = _http("GET", "/api/workspace/state")
    result.pop("browser_capability", None)
    result["url"] = _browser_url(result)
    result.pop("browser_url", None)
    result["browser_opened"] = False
    if open_browser and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        try:
            result["browser_opened"] = bool(webbrowser.open(result["url"], 1, True))
        except Exception:
            logging.exception("Could not open the visual workbench")
    return result


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True))
def workspace_get_context() -> CallToolResult:
    """Read this project's current scene revision, reference photos and Codex thread identity."""
    ensure_http_server()
    context = _http("GET", "/api/workspace/context")
    blocks: list[TextContent | ImageContent] = []
    for reference in context["reference_images"]:
        reference["path"] = str(_image_path(reference["url"]))
    if context.get("reference_clip"):
        from dynamic import reference_frames
        for frame in reference_frames(context["reference_clip"]):
            frame["path"] = str(_image_path(frame["url"]))
    for obj in context["scene"]["objects"]:
        if obj.get("type") == "model" and obj.get("url", "").startswith("/assets/"):
            obj["local_path"] = str(DATA_DIR / "assets" / obj["url"].rsplit("/", 1)[-1])
    blocks.append(TextContent(type="text", text=json.dumps(context, ensure_ascii=False)))
    for reference in context["reference_images"]:
        blocks.append(TextContent(type="text", text=f"Reference original: {reference['path']}"))
        blocks.append(_preview_image(Path(reference["path"])))
    return CallToolResult(content=blocks, structured_content=context)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True))
def workspace_get_feedback(feedback_id: str, include_details: bool = False) -> CallToolResult:
    """Read compact feedback and relevant images. Set include_details for full archived fields."""
    if type(include_details) is not bool:
        raise ValueError("include_details must be a boolean")
    ensure_http_server()
    packet = _http("GET", f"/api/workspace/feedback/{feedback_id}")
    return _visual_tool_result({"items": [packet], "next_cursor": 1, "session_id": packet["session_id"]}, include_details=include_details)


@mcp.tool()
def workspace_export_pose_sources(request_id: str | None = None) -> dict[str, Any]:
    """Export every current reference camera/frame for an external human reconstruction skill.

    No inference runs here. Returns a server-local manifest_json_path, job_id,
    track_id and exact source_snapshot_id bound to this project/session/media.
    Preserve manifest frame IDs, dimensions, view IDs, frame_index and time_seconds.
    Use capsule-human-tracking to generate evidence, then import its JSON result.
    Reuse request_id to retry the same export. Scene geometry can change meanwhile;
    replacing reference media requires a new export.
    """
    ensure_http_server()
    payload = {"request_id": request_id} if request_id is not None else {}
    return _http("POST", "/api/workspace/pose/sources", payload, private=True, timeout=60)


@mcp.tool()
def workspace_import_human_pose(job_id: str, result_path: str | None = None,
                                result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Import validated external 2D observations or explicitly labeled 3D projections.

    Provide exactly one result_path (JSON file within this project on the server)
    or result object. Echo the exported job_id, track_id, project_id, session_id,
    source_snapshot_id, schema_version, view_id(s) and exact per-frame identity.
    Declare evidence_kind observed_2d or projected_3d; normalized joint coordinates
    cannot be guessed or mapped between named profiles. Optional keypoint_profile,
    keypoint_names and skeleton_edges support custom joint layouts (default COCO17).
    No worker, model downloads or GPU inference are part of this tool.
    """
    ensure_http_server()
    if not isinstance(job_id, str) or re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
        raise ValueError("job_id must be a 32-character hexadecimal ID")
    if (result_path is None) == (result is None):
        raise ValueError("provide exactly one result_path or result")
    payload = {"job_id": job_id, **({"result_path": result_path} if result_path is not None else {"result": result})}
    return _http("POST", "/api/workspace/pose/import", payload, private=True, timeout=60)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True))
def workspace_get_human_pose(job_id: str | None = None, frame_offset: int = 0, max_frames: int = 8,
                             view_id: str | None = None) -> dict[str, Any]:
    """Read imported or historical human-pose evidence for this project.

    Without job_id, list current-session exports and imported results.
    Completed jobs include normalized coordinates, confidence, source IDs, view,
    frame, time, camera, and skeleton edges. Read up to max_frames samples
    (default 8, maximum 32) from frame_offset; next_frame_offset paginates them.
    Set view_id to retrieve one camera's samples from a multi-camera job.
    result_json_path identifies the complete imported result on disk. Preserve profile and evidence_kind provenance.
    """
    ensure_http_server()
    if job_id is None and view_id is not None:
        raise ValueError("view_id filtering requires a pose job_id")
    if job_id is not None:
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise ValueError("job_id must be a 32-character hexadecimal ID")
        if type(frame_offset) is not int or frame_offset < 0 or type(max_frames) is not int or not 1 <= max_frames <= 32:
            raise ValueError("frame_offset must be nonnegative; max_frames must be between 1 and 32")
        parameters = {"frame_offset": frame_offset, "max_frames": max_frames}
        if view_id is not None:
            if not isinstance(view_id, str) or re.fullmatch(r"[0-9a-f]{32}", view_id) is None:
                raise ValueError("view_id must be a 32-character hexadecimal camera ID")
            parameters["view_id"] = view_id
        result = _http("GET", f"/api/workspace/pose/{job_id}?{urlencode(parameters)}")
        return result
    return _http("GET", "/api/workspace/pose")


@mcp.tool()
def workspace_publish_scene(local_path: str, expected_revision: int) -> dict[str, Any]:
    """Publish a self-contained GLB after editing its source; refresh the same workbench."""
    ensure_http_server()
    return _http("POST", "/api/workspace/publish", {"local_path": local_path, "expected_revision": expected_revision}, private=True)


@mcp.tool()
def workspace_set_reference_clip(manifest_path: str | None = None, video_path: str | None = None,
                                 fps: float | None = None, camera_manifest_path: str | None = None,
                                 clear: bool = False, append_view: bool = False,
                                 view_name: str | None = None, replace_view_id: str | None = None) -> dict[str, Any]:
    """Load a project-local image-sequence manifest or video for synchronized dynamic review.

    Sequence JSON: {name?,fps?,duration_sec?,frames:[{path,time_sec?,camera?}]}.
    Paths are relative to the manifest and must remain within the project.
    Videos are fully sampled into at most 600 frames (ffmpeg required); fps defaults to 10.
    An optional video camera manifest has one camera or timed frames[{time_sec,camera}].
    A multi-view manifest has views:[{name?,manifest_path|video_path|frames,fps?,duration_sec?,camera_manifest_path?}].
    append_view=True adds views while preserving the primary clip ID and existing frames (up to 8 views).
    replace_view_id updates one existing view. view_name labels a single imported view.
    clear=True removes the current clip. This does not change scene revision.
    """
    ensure_http_server()
    payload = {"clear": True} if clear else {key: value for key, value in {"manifest_path": manifest_path, "video_path": video_path, "fps": fps, "camera_manifest_path": camera_manifest_path, "append_view": append_view, "view_name": view_name, "replace_view_id": replace_view_id}.items() if value is not None}
    return _http("POST", "/api/workspace/clip", payload, private=True, timeout=120)


@mcp.tool()
def workspace_request_feedback(message: str, object_ids: list[str] | None = None) -> dict[str, Any]:
    """Ask the human to inspect a result in the workbench, then return immediately."""
    ensure_http_server()
    state = _http("GET", "/api/workspace/state")
    if state.get("delivery_mode") == "external":
        result = _http("POST", "/api/workspace/external/request", {"message": message}, private=True)
        result["url"] = _browser_url(state)
        return result
    return _http("POST", "/api/workspace/request-feedback", {"message": message, "object_ids": object_ids}, private=True)


@mcp.tool()
async def request_visual_feedback(
    reference_images: list[str] | None = None,
    current_scene: dict[str, Any] | None = None,
    scene_glb_path: str | None = None,
    session_id: str | None = None,
    cursor: int | None = None,
    wait_for_submit: bool = True,
    timeout_sec: int = 600,
    open_browser: bool = True,
) -> CallToolResult:
    """Enter human visual debugging in the invoking Codex task and return the annotated feedback.

    Use this for '进入人工调试模式' during 3D reconstruction. Pass reference_images
    as project-local PNG/JPEG paths and scene_glb_path as a project-local GLB.
    The persistent workbench session is bound to the configured project.
    """
    state = _external_state()
    if session_id is not None and session_id != state["session_id"]:
        raise ValueError("session_id belongs to a different workspace")
    if current_scene is not None and scene_glb_path is not None:
        raise ValueError("provide current_scene or scene_glb_path, not both")
    if current_scene is not None and (not isinstance(current_scene, dict) or not isinstance(current_scene.get("objects"), list)):
        raise ValueError("current_scene must contain an objects array")
    if type(timeout_sec) is not int or not 1 <= timeout_sec <= 3600:
        raise ValueError("timeout_sec must be between 1 and 3600")
    if cursor is not None and (type(cursor) is not int or cursor < 0):
        raise ValueError("cursor must be a nonnegative integer")
    if reference_images is not None:
        await asyncio.to_thread(_http, "POST", "/api/workspace/references", {"reference_images": reference_images}, private=True)
    if scene_glb_path is not None:
        scene = await asyncio.to_thread(_http, "GET", "/api/scene")
        await asyncio.to_thread(_http, "POST", "/api/workspace/publish", {"local_path": scene_glb_path, "expected_revision": scene["revision"]}, private=True)
    elif current_scene is not None:
        scene = await asyncio.to_thread(_http, "GET", "/api/scene")
        await asyncio.to_thread(_http, "PUT", "/api/scene", {"expected_revision": scene["revision"], "objects": current_scene["objects"]}, private=True)
    request = await asyncio.to_thread(_http, "POST", "/api/workspace/external/request", {"session_id": state["session_id"], "cursor": cursor, "message": "请在参考图和当前场景上标出要调整的位置，然后发送给当前 Codex 任务。"}, private=True)
    effective_cursor = request["next_cursor"]
    url = _browser_url(state)
    opened = False
    if open_browser and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        try:
            opened = bool(await asyncio.wait_for(asyncio.to_thread(webbrowser.open, url, 1, True), timeout=5))
        except Exception:
            logging.exception("Could not open the visual workbench")
    if wait_for_submit and not state.get("thread_id"):
        result = await _wait_for_external_feedback(state["session_id"], timeout_sec, effective_cursor, url)
        result["browser_opened"] = opened
        return _visual_tool_result(result)
    response = {**request, "url": url, "browser_opened": opened, "reference_images": _http("GET", "/api/workspace/context")["reference_images"]}
    if state.get("thread_id"):
        response["message"] = "The workbench is bound to this Codex Desktop task. After the user submits, the service will add the visual feedback as a new user turn in this task; no MCP wait is needed."
    else:
        response["message"] = "Open the URL and submit your marks. Call wait_visual_feedback with session_id and next_cursor if this invocation did not wait."
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(response, ensure_ascii=False))], structured_content=response)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True))
async def wait_visual_feedback(session_id: str, cursor: int = 0, timeout_sec: int = 600) -> CallToolResult:
    """Wait for the next visual feedback packet and return images to this same Codex task."""
    state = _external_state()
    if session_id != state["session_id"]:
        raise ValueError("session_id belongs to a different workspace")
    if state.get("thread_id"):
        raise ValueError("this workbench delivers feedback directly to its bound Codex Desktop task; no MCP wait is needed")
    await asyncio.to_thread(_http, "POST", "/api/workspace/external/request", {"session_id": session_id, "cursor": cursor}, private=True)
    return _visual_tool_result(await _wait_for_external_feedback(session_id, timeout_sec, cursor, _browser_url(state)))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True))
def get_visual_feedback(session_id: str, cursor: int = 0) -> CallToolResult:
    """Read submitted visual feedback now, including the original and annotated images."""
    state = _external_state()
    if session_id != state["session_id"]:
        raise ValueError("session_id belongs to a different workspace")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a nonnegative integer")
    query = urlencode({"session_id": session_id, "cursor": cursor})
    result = _http("GET", f"/api/workspace/external/feedback?{query}", private=True)
    result["url"] = _browser_url(state)
    return _visual_tool_result(result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mcp.run()
