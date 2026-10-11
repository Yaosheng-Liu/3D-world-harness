#!/usr/bin/env python3
"""Import ready folders through a real isolated workbench and HTTP server.

Uses only temporary PNG/GLB fixtures and an MCP-event workspace. No Codex model
is contacted and no feedback is submitted.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts"), str(ROOT / "tests")]

from dynamic import reference_views
from gateway import WorkspaceGateway
from server import make_server
from test_object_double_click_browser import model
from test_prompt_drag_browser import image_data
from workspace_ui_helpers import control


def camera(x: float) -> dict:
    return {
        "camera_to_world": [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]],
        "intrinsics": {"width": 320, "height": 240, "fx": 300, "fy": 300, "cx": 160, "cy": 120},
        "image_undistorted": True,
    }


def png(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(image_data(color).split(",", 1)[1]))


def source_files(directory: Path) -> dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def fixtures(parent: Path) -> tuple[Path, Path, list[dict]]:
    parent.mkdir()
    dynamic = parent / "dynamic_ready"
    static = parent / "static_ready"
    dynamic.mkdir()
    model(dynamic / "dynamic_scene.glb")
    (dynamic / "dynamic_scene.blend").write_bytes(b"editable Blender source fixture")
    (dynamic / "camera_manifest.json").write_text(json.dumps({"camera": camera(0)}), encoding="utf-8")
    (static / "output").mkdir(parents=True)
    model(static / "output" / "scene.glb")
    views = []
    for name, folder, fps, times, colors, offset in (
        ("GT 正面", "front", 2, [0, 0.5], ["navy", "blue"], 0),
        ("GT 侧面", "side", 4, [0, 0.75], ["maroon", "red"], 1),
    ):
        frames = []
        for index, (time, color) in enumerate(zip(times, colors)):
            relative = f"{folder}/frame{index}.png"
            png(dynamic / "references" / relative, color)
            frames.append({"path": relative, "name": f"{folder}{index}.png", "time_sec": time,
                           "camera": camera(offset + index * 0.1)})
        views.append({"name": name, "fps": fps, "duration_sec": 1.25, "frames": frames})
    # The marker path is relative to the instance; each frame path is relative
    # to the containing manifest, matching the supported ready format.
    manifest = dynamic / "references" / "multiview.json"
    manifest.write_text(json.dumps({"name": "GT 多机位", "views": views}, ensure_ascii=False), encoding="utf-8")
    (dynamic / "workbench-ready.json").write_text(json.dumps({
        "status": "ready", "name": "动态 ready 实例", "scene_glb": "dynamic_scene.glb",
        "editable_blend": "dynamic_scene.blend", "reference_sequence": "references/multiview.json",
        "world_up": "Z",
    }, ensure_ascii=False), encoding="utf-8")
    png(static / "references" / "static.png", "green")
    (static / "workbench_ready.json").write_text(json.dumps({
        "schema_version": 1, "name": "静态 ready 实例", "scene_glb_path": "output/scene.glb",
        "reference_images": ["references/static.png"],
    }, ensure_ascii=False), encoding="utf-8")
    ambiguous = parent / "ambiguous"
    (ambiguous / "output").mkdir(parents=True)
    model(ambiguous / "output" / "first.glb")
    model(ambiguous / "output" / "second.glb")
    png(ambiguous / "references" / "frame.png", "black")
    (ambiguous / "references" / "multiview.json").write_text(json.dumps({
        "views": [{"name": "ambiguous", "fps": 1, "frames": [{"path": "frame.png", "time_sec": 0}]}],
    }), encoding="utf-8")
    not_ready = parent / "not_ready"
    not_ready.mkdir()
    (not_ready / "workbench-ready.json").write_text('{"schema_version": 1, "ready": false}', encoding="utf-8")
    return dynamic, static, views


def model_catalog(parent: Path) -> list[Path]:
    """A parent manifest names model-only children, as in the user's bundle."""
    parent.mkdir()
    instances, entries = [], []
    for number, name in enumerate(("目录铲车", "目录消防车"), start=1):
        instance = parent / f"0{number}_vehicle"
        instance.mkdir()
        model(instance / "scene.glb")
        (instance / "scene.blend").write_bytes(f"editable catalog source {number}".encode())
        instances.append(instance)
        entries.append({"name": name, "folder": instance.name,
                        "glb": f"{instance.name}/scene.glb", "blend": f"{instance.name}/scene.blend"})
    (parent / "manifest.json").write_text(json.dumps({"scenes": entries}, ensure_ascii=False), encoding="utf-8")
    return instances


def http(base: str, path: str, *, payload=None, capability=None) -> tuple[int, dict]:
    headers = {}
    if capability is not None:
        headers["X-Workspace-Capability"] = capability
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    try:
        response = build_opener(ProxyHandler({})).open(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def ready(page) -> None:
    page.wait_for_function("window.__folderCheck?.state.workspaceReady && !__folderCheck.state.sceneLoading && "
                           "!__folderCheck.state.seeking && document.getElementById('reference-image').naturalWidth > 0")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    cached_browser = Path("/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome")
    parser.add_argument("--browser-executable", default=str(cached_browser) if cached_browser.is_file() else None)
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="folder-import-browser-") as temporary:
        root = Path(temporary)
        original = root / "original"
        original.mkdir()
        dynamic, static, expected_views = fixtures(root / "instances")
        before_sources = source_files(root / "instances")
        catalog_instances = model_catalog(root / "model_catalog")
        before_catalog = source_files(root / "model_catalog")
        model(original / "original.glb")
        png(original / "original.png", "gray")
        errors, browser_writes, browser_reads, imported_requests = [], [], [], []
        with patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("folder import created a model task")) as target_mock, \
             patch.object(WorkspaceGateway, "list_models", side_effect=AssertionError("folder import requested models")) as models_mock:
            server = make_server(port=0, data_dir=root / "data", project_dir=original, web_dir=ROOT / "web",
                                 external_review=True, feedback_transport="mcp_events")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            store, gateway, registry = server.scene_store, server.workspace_gateway, server.project_registry
            session_id = gateway.ensure()["session_id"]
            store.import_model(str(original / "original.glb"), object_id="original_model", name="原始模型")
            gateway.add_reference_paths([str(original / "original.png")])
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with patch.object(registry, "_factory", wraps=registry._factory) as factory, sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True,
                        **({"executable_path": args.browser_executable} if args.browser_executable else {}),
                        args=["--no-sandbox", "--no-proxy-server", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    hook = "\nwindow.__folderCheck={state,ui,renderer,cameraData,workspacePrefix,referenceViews};"
                    context.route("**/app.js", lambda route: route.fulfill(status=200, content_type="application/javascript",
                                  body=(ROOT / "web" / "app.js").read_text() + hook))
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def track_request(request):
                        if request.method == "GET":
                            browser_reads.append(urlsplit(request.url).path)
                        if request.method not in {"GET", "HEAD", "OPTIONS"}:
                            browser_writes.append((request.method, urlsplit(request.url).path))
                        if request.method == "POST" and urlsplit(request.url).path.endswith("/api/projects/import-folder"):
                            imported_requests.append(request.post_data_json)

                    page.on("request", track_request)
                    page.goto(server.browser_url(session_id))
                    ready(page)
                    expect(page.locator("#sidebar-open-folder")).to_be_hidden()
                    status, state = http(base, "/api/workspace/state")
                    assert status == 200 and state["project_folder_import_supported"] is True
                    root_capability = state["browser_capability"]
                    assert state["feedback_transport"] == "mcp_events"
                    note = page.locator("#feedback-note")
                    if not note.is_visible():
                        control(page, "#chat-launcher").click()
                    draft = "未提交草稿：保留原始场景和参考图。"
                    note.fill(draft)
                    original_scene = copy.deepcopy(store.scene())
                    original_session = copy.deepcopy(store.get_session(session_id))
                    original_camera = page.evaluate("JSON.stringify(__folderCheck.cameraData())")
                    original_url = page.url
                    page.locator("#projects-dialog-button").click()
                    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase === 'open'")
                    expect(page.locator("#sidebar-open-folder")).to_be_visible()
                    page.locator("#sidebar-open-folder").click()
                    folder_path = page.locator("#folder-import-path")
                    expect(folder_path).to_be_enabled()
                    folder_path.fill(str(root / "instances"))
                    # Browse and leave the page before importing: no scene action.
                    page.locator("#folder-import-browse").click()
                    expect(page.locator("#folder-import-directories button")).to_have_count(4)
                    page.screenshot(path="/tmp/scene_feedback_folder_import_050.png")
                    page.locator('#sidebar-folder-import [data-sidebar-home]').click()
                    assert not imported_requests and store.scene() == original_scene
                    page.locator("#sidebar-open-folder").click()
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/projects/import-folder")) as importing:
                        folder_path.press("Enter")
                    response = importing.value
                    assert response.status == 200
                    result = response.json()
                    assert (len(result["imported"]), len(result["errors"])) == (2, 1)
                    expect(page.locator("#folder-import-result")).to_contain_text("已导入 2")
                    expect(page.locator("#folder-import-result")).to_contain_text("失败 1")
                    page.locator(".folder-import-errors > summary").click()
                    expect(page.locator(".folder-import-errors")).to_contain_text("ambiguous")
                    expect(page.locator("#folder-import-submit")).to_be_enabled()
                    assert len(imported_requests) == 1 and re.fullmatch(r"[0-9a-f]{32}", imported_requests[0]["request_id"])
                    assert page.url == original_url
                    assert page.evaluate("__folderCheck.state.sessionId") == session_id
                    assert page.evaluate("JSON.stringify(__folderCheck.cameraData())") == original_camera
                    assert note.input_value() == draft
                    assert store.scene() == original_scene and store.get_session(session_id) == original_session
                    page.locator('#sidebar-folder-import [data-sidebar-home]').click()
                    expect(page.locator("#project-list .project-item")).to_have_count(3)
                    print("PASS real sidebar browse/cancel/Enter: two imports, one visible error; current scene, session, camera and draft remain intact", flush=True)

                    # Same-request and fresh-request retries both reuse source contexts.
                    for request_id in (imported_requests[0]["request_id"], uuid.uuid4().hex):
                        status, retry = http(base, "/api/projects/import-folder", payload={"path": str(root / "instances"), "request_id": request_id}, capability=root_capability)
                        assert status == 200 and retry["imported"] == []
                        assert len(retry["projects"]) == 2 and len(retry["errors"]) == 1
                    assert factory.call_count == 2
                    assert all(call.args[0]["kind"] == "imported" and call.args[1] is True for call in factory.call_args_list)
                    contexts = {context.project_dir: context for context in registry.contexts()[1:]}
                    assert set(contexts) == {dynamic, static}
                    dynamic_context, static_context = contexts[dynamic], contexts[static]
                    for imported_context in contexts.values():
                        workspace = imported_context.store.workspace()
                        assert imported_context.gateway.adapter is None
                        assert workspace["thread_id"] is None and workspace["created_thread_ids"] == []
                        assert workspace["queue"] == [] and imported_context.store.state["feedback"] == []
                        assert imported_context.store.browser_token != root_capability
                        assert imported_context.store.control_token != store.control_token
                        assert imported_context.store.data_dir == root / "data" / "projects" / imported_context.project_id / "data"
                    dynamic_session = dynamic_context.store.get_session(dynamic_context.gateway.ensure()["session_id"])
                    assert dynamic_context.store.scene()["objects"][0]["metadata"]["up_axis"] == "z"
                    imported_views = reference_views(dynamic_session["reference_clip"])
                    assert len(imported_views) == 2
                    for expected, actual in zip(expected_views, imported_views):
                        assert (actual["name"], actual["fps"], actual["duration_sec"]) == (expected["name"], expected["fps"], expected["duration_sec"])
                        assert [frame["time_sec"] for frame in actual["frames"]] == [frame["time_sec"] for frame in expected["frames"]]
                        for source, copied in zip(expected["frames"], actual["frames"]):
                            assert copied["camera"]["camera_to_world"] == source["camera"]["camera_to_world"]
                            assert copied["camera"]["intrinsics"] == source["camera"]["intrinsics"]
                            assert copied["camera"]["image_undistorted"] is True
                            destination = dynamic_context.store.media_dir / copied["url"].rsplit("/", 1)[-1]
                            assert destination.read_bytes() == (dynamic / "references" / source["path"]).read_bytes()
                    static_session = static_context.store.get_session(static_context.gateway.ensure()["session_id"])
                    assert static_session["reference_clip"] is None and len(static_session["reference_images"]) == 1
                    for imported_context in contexts.values():
                        asset = imported_context.store.scene()["objects"][0]
                        source_glb = dynamic / "dynamic_scene.glb" if imported_context is dynamic_context else static / "output" / "scene.glb"
                        assert (imported_context.store.assets_dir / asset["url"].rsplit("/", 1)[-1]).read_bytes() == source_glb.read_bytes()
                    assert source_files(root / "instances") == before_sources
                    print("PASS repeat requests deduplicate without new contexts, adapters or tasks; copied GLB/PNG bytes, cameras and frame times are exact", flush=True)

                    for endpoint in ("/api/projects/folders", "/api/projects/import-folder"):
                        payload = {"path": str(root / "instances")}
                        if endpoint.endswith("import-folder"):
                            payload["request_id"] = uuid.uuid4().hex
                        status, _ = http(base, endpoint, payload=payload)
                        assert status == 403
                    invalid = [{"path": str(dynamic)}, {"path": "relative", "request_id": uuid.uuid4().hex},
                               {"path": str(dynamic), "request_id": "invalid"},
                               {"path": str(dynamic), "request_id": uuid.uuid4().hex, "model": "unrequested-model"},
                               {"path": "\0", "request_id": uuid.uuid4().hex}, []]
                    for payload in invalid:
                        status, _ = http(base, "/api/projects/import-folder", payload=payload, capability=root_capability)
                        assert status == 400
                    dynamic_prefix = f"/p/{dynamic_context.project_id}"
                    status, _ = http(base, dynamic_prefix + "/api/projects/folders", payload={"path": str(dynamic)}, capability=root_capability)
                    assert status == 403
                    status, _ = http(base, "/api/projects/folders", payload={"path": str(dynamic)}, capability=dynamic_context.store.browser_token)
                    assert status == 403
                    status, _ = http(base, dynamic_prefix + "/api/projects/folders", payload={"path": str(dynamic)}, capability=dynamic_context.store.browser_token)
                    assert status == 200
                    status, _ = http(base, f"/api/sessions/{dynamic_session['session_id']}")
                    assert status == 404
                    assert factory.call_count == 2
                    print("PASS advertised folder capability; missing and cross-project credentials rejected, malformed imports return 400, sessions stay in their namespace", flush=True)

                    dynamic_metadata = next(item for item in result["projects"] if item["project_id"] == dynamic_context.project_id)
                    page.locator("#project-list .project-item").filter(has_text=dynamic_metadata["name"]).click()
                    page.wait_for_url(re.compile(re.escape(base + dynamic_prefix) + r"/.*"))
                    ready(page)
                    assert page.evaluate("__folderCheck.workspacePrefix") == dynamic_prefix
                    assert page.evaluate("__folderCheck.state.projectId") == dynamic_context.project_id
                    assert page.evaluate("__folderCheck.state.browserCapability === " + json.dumps(dynamic_context.store.browser_token))
                    page.wait_for_function("[...__folderCheck.state.objectNodes.values()].some(node=>node.userData.loaded && node.userData.gltfRoot)")
                    assert page.evaluate("__folderCheck.state.detectedUpAxis") == "z"
                    page.wait_for_function("__folderCheck.renderer.info.render.triangles > 0")
                    assert page.evaluate("[...__folderCheck.state.objectNodes.values()][0].userData.gltfRoot.children[0].name") == "Cabinet"
                    expect(page.locator("#reference-strip button[data-view-id]")).to_have_count(2)
                    assert dynamic_prefix + "/media/" in page.locator("#reference-image").get_attribute("src")
                    expect(page.locator("#feedback-note")).to_have_value("")
                    page.locator("#reference-strip button[data-view-id]").nth(1).click()
                    page.wait_for_function("__folderCheck.state.activeViewId === __folderCheck.referenceViews()[1].clip_id && !__folderCheck.state.seeking")
                    page.locator("#timeline-next").click()
                    page.wait_for_function("!__folderCheck.state.seeking && __folderCheck.state.time === 0.75")
                    assert page.evaluate("__folderCheck.referenceViews()[1].frames[1].camera.camera_to_world") == expected_views[1]["frames"][1]["camera"]["camera_to_world"]
                    # Cached frames may display a blob URL; their fetch remains
                    # scoped to this project, and displayed pixels must match GT.
                    assert dynamic_prefix + imported_views[1]["frames"][1]["url"] in browser_reads
                    assert page.evaluate("""()=>{const image=document.getElementById('reference-image'),canvas=document.createElement('canvas');
                        canvas.width=1;canvas.height=1;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
                        return [...ctx.getImageData(0,0,1,1).data];}""") == [255, 0, 0, 255]
                    page.locator("#projects-dialog-button").click()
                    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase === 'open'")
                    page.locator("#project-list .project-item").filter(has_text=registry.root.name).click()
                    page.wait_for_url(re.compile(re.escape(base + "/p/" + registry.root.project_id) + r"/.*"))
                    ready(page)
                    assert page.evaluate("__folderCheck.state.sessionId") == session_id
                    expect(page.locator("#feedback-note")).to_have_value(draft)
                    assert store.scene() == original_scene and store.get_session(session_id) == original_session
                    assert source_files(root / "instances") == before_sources

                    # A parent manifest can import explicitly listed model-only
                    # children without making up reference evidence or a task.
                    page.locator("#projects-dialog-button").click()
                    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase === 'open'")
                    page.locator("#sidebar-open-folder").click()
                    folder_path = page.locator("#folder-import-path")
                    expect(folder_path).to_be_enabled()
                    folder_path.fill(str(root / "model_catalog"))
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/projects/import-folder")) as importing:
                        folder_path.press("Enter")
                    catalog_result = importing.value.json()
                    assert (len(catalog_result["imported"]), len(catalog_result["errors"])) == (2, 0), catalog_result
                    expect(page.locator("#folder-import-result")).to_contain_text("其中 2 个实例只包含模型，尚无参考图")
                    assert note.input_value() == draft
                    assert store.scene() == original_scene and store.get_session(session_id) == original_session
                    status, catalog_retry = http(base, "/api/projects/import-folder", payload={
                        "path": str(root / "model_catalog"), "request_id": imported_requests[-1]["request_id"]}, capability=root_capability)
                    assert status == 200 and catalog_retry["imported"] == [] and len(catalog_retry["projects"]) == 2
                    assert factory.call_count == 4
                    all_contexts = {context.project_dir: context for context in registry.contexts()[1:]}
                    for instance in catalog_instances:
                        imported_context = all_contexts[instance]
                        session = imported_context.store.get_session(imported_context.gateway.ensure()["session_id"])
                        assert session["reference_clip"] is None and session["reference_images"] == []
                        workspace = imported_context.store.workspace()
                        assert workspace["thread_id"] is None and workspace["created_thread_ids"] == [] and workspace["queue"] == []
                        assert imported_context.gateway.adapter is None
                        assert imported_context.store.browser_token != root_capability
                        asset = imported_context.store.scene()["objects"][0]
                        assert (imported_context.store.assets_dir / asset["url"].rsplit("/", 1)[-1]).read_bytes() == (instance / "scene.glb").read_bytes()
                    page.locator('#sidebar-folder-import [data-sidebar-home]').click()
                    expect(page.locator("#project-list .project-item")).to_have_count(5)
                    catalog_metadata = catalog_result["projects"][0]
                    page.locator("#project-list .project-item").filter(has_text=catalog_metadata["name"]).click()
                    catalog_prefix = "/p/" + catalog_metadata["project_id"]
                    page.wait_for_url(re.compile(re.escape(base + catalog_prefix) + r"/.*"))
                    page.wait_for_function("window.__folderCheck?.state.workspaceReady && !__folderCheck.state.sceneLoading && "
                                           "[...__folderCheck.state.objectNodes.values()].some(node=>node.userData.loaded && node.userData.gltfRoot) && "
                                           "__folderCheck.renderer.info.render.triangles > 0")
                    assert page.evaluate("__folderCheck.workspacePrefix") == catalog_prefix
                    expect(page.locator("#reference-empty")).to_be_visible()
                    expect(page.locator("#reference-media")).to_be_hidden()
                    page.locator("#projects-dialog-button").click()
                    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase === 'open'")
                    page.locator("#project-list .project-item").filter(has_text=registry.root.name).click()
                    page.wait_for_url(re.compile(re.escape(base + "/p/" + registry.root.project_id) + r"/.*"))
                    ready(page)
                    expect(page.locator("#feedback-note")).to_have_value(draft)
                    assert store.scene() == original_scene and store.get_session(session_id) == original_session
                    assert source_files(root / "model_catalog") == before_catalog
                    print("PASS parent model-only catalog imports named scenes once, renders real GLB with an empty reference panel, preserves sources/root draft and creates no tasks", flush=True)

                    assert all(path.endswith("/api/projects/folders") or path.endswith("/api/projects/import-folder") for _, path in browser_writes)
                    assert store.state["feedback"] == [] and not errors, errors
                    target_mock.assert_not_called()
                    models_mock.assert_not_called()
                    print("PASS imported project opens its real GLB and two GT views in its namespace, next frame is 0.75s, returning restores the root draft; no browser errors or model requests", flush=True)
                    browser.close()
            finally:
                server.shutdown()
                worker.join(5)
                server.server_close()
    print("ALL FOLDER IMPORT BROWSER CHECKS PASSED", flush=True)


if __name__ == "__main__":
    main()
