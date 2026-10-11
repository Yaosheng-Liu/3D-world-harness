#!/usr/bin/env python3
"""Exercise automatic ready validation on every folder import over real HTTP."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts"), str(ROOT / "tests")]

from gateway import WorkspaceGateway
from server import make_server
from test_folder_import_browser import camera, fixtures, http, model_catalog, png, source_files
from test_object_double_click_browser import model
from test_scene_unload_browser import draft, ready, sidebar


def bundle(root: Path) -> Path:
    path = root / "bundle"
    path.mkdir()
    dynamic, _, _ = fixtures(root / "original_fixtures")
    dynamic.rename(path / "dynamic_ready")
    model_catalog(path / "model_catalog")
    manifest = path / "model_catalog" / "manifest.json"
    document = json.loads(manifest.read_text())
    document["scenes"][0]["name"] = '<img src=x onerror="window.__readyInjected=1">'
    manifest.write_text(json.dumps(document), encoding="utf-8")
    bad_glb = path / "bad_glb"
    bad_glb.mkdir()
    (bad_glb / "scene.glb").write_bytes(b"This is not a GLB container.")
    png(bad_glb / "frame.png", "green")
    (bad_glb / "workbench-ready.json").write_text(json.dumps({
        "schema_version": 1, "name": "损坏模型", "scene_glb_path": "scene.glb", "reference_images": ["frame.png"],
    }), encoding="utf-8")
    bad_camera = path / "bad_camera"
    bad_camera.mkdir()
    model(bad_camera / "scene.glb")
    png(bad_camera / "frame.png", "navy")
    invalid = camera(0)
    invalid["intrinsics"]["fx"] = -1
    (bad_camera / "reference.json").write_text(json.dumps({"fps": 1, "frames": [
        {"path": "frame.png", "time_sec": 0, "camera": invalid},
    ]}), encoding="utf-8")
    (bad_camera / "workbench-ready.json").write_text(json.dumps({
        "schema_version": 1, "name": "错误相机", "scene_glb_path": "scene.glb", "reference_manifest": "reference.json",
    }), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    executable = Path("/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome")
    parser.add_argument("--browser-executable", default=str(executable) if executable.is_file() else None)
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="ready-gate-browser-") as temporary:
        root = Path(temporary)
        parent = bundle(root)
        before_source = source_files(parent)
        only_blocked = root / "only_blocked"
        shutil.copytree(parent / "bad_glb", only_blocked)
        blocked_source = source_files(only_blocked)
        unknown = root / "unrecognized"
        unknown.mkdir()
        (unknown / "note.txt").write_text("No declared workbench instance.", encoding="utf-8")
        original = root / "original"
        original.mkdir()
        model(original / "original.glb")
        png(original / "original.png", "gray")
        errors, checks, imports, writes, dialogs = [], [], [], [], []
        with patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("import created a task")) as targets, \
             patch.object(WorkspaceGateway, "list_models", side_effect=AssertionError("import requested models")) as models:
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
                    hook = "\nwindow.__unloadCheck={state,cameraData,renderer};window.__readyCheck={state,workspaceFolderImport};"
                    context.route("**/app.js", lambda route: route.fulfill(status=200, content_type="application/javascript",
                                  body=(ROOT / "web" / "app.js").read_text() + hook))
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))

                    def track(call):
                        if call.method not in {"GET", "HEAD", "OPTIONS"}:
                            writes.append((call.method, call.url))
                        if call.url.endswith("/api/projects/check-folder"):
                            checks.append(call.post_data_json)
                        if call.url.endswith("/api/projects/import-folder"):
                            imports.append(call.post_data_json)

                    page.on("request", track)
                    page.goto(server.browser_url(session_id))
                    ready(page)
                    prompt = "自动检查并导入时保留这一轮的提示草稿。"
                    draft(page, prompt)
                    original_url = page.url
                    original_scene = copy.deepcopy(store.scene())
                    original_session = copy.deepcopy(store.get_session(session_id))
                    camera_before = page.evaluate("JSON.stringify(__unloadCheck.cameraData())")
                    sidebar(page)
                    page.locator("#sidebar-open-folder").click()
                    entry = page.locator("#folder-import-path")
                    expect(page.locator("#folder-import-submit")).to_be_enabled()
                    expect(page.locator("#folder-import-check")).to_have_count(0)
                    entry.fill(str(only_blocked))
                    before_state = store.state_path.read_bytes()
                    before_registry = copy.deepcopy(registry._records)
                    status, _ = http(base, "/api/projects/import-folder", payload={"path": str(parent), "request_id": "a" * 32})
                    assert status == 403
                    capability = page.evaluate("__readyCheck.state.browserCapability")
                    status, _ = http(base, "/api/projects/import-folder", payload={"path": str(parent), "request_id": "a" * 32, "model": "unrequested"}, capability=capability)
                    assert status == 400

                    endpoint = re.compile(r"/api/projects/import-folder$")
                    held = []

                    def hold(route):
                        response = route.fetch()
                        assert response.status == 200, response.text()
                        held.append((route, response))
                        page.evaluate("window.__readyHeldCount="+str(len(held)))

                    page.evaluate("window.__readyHeldCount=0")
                    page.route(endpoint, hold)
                    page.locator("#folder-import-submit").click()
                    page.wait_for_function("window.__readyHeldCount===1")
                    expect(page.locator("#folder-import-submit")).to_be_disabled()
                    expect(page.locator("#folder-import-browse")).to_be_disabled()
                    expect(entry).to_be_disabled()
                    assert page.evaluate("__readyCheck.workspaceFolderImport.importFolder()") is None
                    assert page.evaluate("__readyCheck.workspaceFolderImport.load('/tmp')") is None
                    page.locator(".folder-import-form").evaluate("el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
                    blocked_result = held[0][1].json()
                    assert blocked_result["ready_check"]["status"] == "blocked" and not blocked_result["ready_check"]["can_import"]
                    assert blocked_result["imported"] == [] and len(blocked_result["errors"]) == 1
                    assert factory.call_count == 0 and registry._records == before_registry
                    held[0][0].fulfill(response=held[0][1])
                    page.unroute(endpoint, hold)
                    expect(page.locator("#folder-import-submit")).to_be_enabled()
                    expect(page.locator("#folder-import-result")).to_contain_text("失败 1")
                    expect(page.locator("#folder-import-result")).to_contain_text("模型无法导入")
                    assert "is-error" in page.locator("#folder-import-result").get_attribute("class")
                    expect(page.locator(".folder-ready-instance")).to_have_count(1)
                    assert len(imports) == 1 and not checks
                    assert store.state_path.read_bytes() == before_state
                    assert len(registry.contexts()) == 1
                    assert source_files(only_blocked) == blocked_source and source_files(parent) == before_source
                    assert page.url == original_url
                    expect(page.locator("#feedback-note")).to_have_value(prompt)
                    print("PASS one automatic validation/import request, no checker button or separate check call; blocked GLB allocates no scene and shows its actual error", flush=True)

                    # Correcting the failed source retries the same saved request.
                    model(only_blocked / "scene.glb")
                    corrected_source = source_files(only_blocked)
                    with page.expect_response(lambda response: response.url.endswith("/api/projects/import-folder")) as recovered:
                        entry.press("Enter")
                    recovered_result = recovered.value.json()
                    assert recovered.value.status == 200
                    assert len(recovered_result["imported"]) == 1 and recovered_result["errors"] == []
                    assert recovered_result["ready_check"]["status"] == "warning" and recovered_result["ready_check"]["can_import"]
                    assert imports[0]["request_id"] == imports[1]["request_id"]
                    expect(page.locator("#folder-import-result")).to_contain_text("已导入 1")
                    expect(page.locator("#folder-import-submit")).to_be_enabled()
                    assert factory.call_count == 1 and source_files(only_blocked) == corrected_source
                    assert page.url == original_url
                    expect(page.locator("#feedback-note")).to_have_value(prompt)
                    print("PASS fixing a blocked source and retrying the same request imports once; nonblocking reference/source warnings require no extra confirmation", flush=True)

                    entry.fill(str(parent))
                    with page.expect_response(lambda response: response.url.endswith("/api/projects/import-folder")) as imported:
                        page.locator("#folder-import-submit").click()
                    result = imported.value.json()
                    report = result["ready_check"]
                    assert imported.value.status == 200
                    assert report["status"] == "partial" and report["can_import"]
                    assert {key: report["counters"][key] for key in ("ready", "warning", "blocked")} == {"ready": 1, "warning": 2, "blocked": 2}, report
                    assert len(result["imported"]) == 3 and len(result["errors"]) == 2
                    expect(page.locator("#folder-import-result")).to_contain_text("已导入 3")
                    expect(page.locator("#folder-import-result")).to_contain_text("失败 2")
                    expect(page.locator("#folder-ready-summary")).to_contain_text("需修复 2")
                    expect(page.locator(".folder-ready-instance")).to_have_count(4)
                    for selector, text in (("bad_glb", "模型无法导入"), ("bad_camera", "参考序列无法导入")):
                        instance = page.locator(".folder-ready-instance").filter(has_text=selector)
                        instance.locator("summary").first.click()
                        expect(instance).to_contain_text(text)
                    assert page.locator("#folder-ready-result img").count() == 0
                    assert page.evaluate("window.__readyInjected || null") is None
                    result_text = page.locator("#folder-ready-result").inner_text()
                    assert "camera_to_world" not in result_text and "intrinsics" not in result_text
                    assert factory.call_count == 4 and len(registry.contexts()) == 5
                    assert source_files(parent) == before_source
                    assert store.state_path.read_bytes() == before_state
                    assert store.scene() == original_scene and store.get_session(session_id) == original_session
                    assert page.url == original_url
                    assert page.evaluate("JSON.stringify(__unloadCheck.cameraData())") == camera_before
                    expect(page.locator("#feedback-note")).to_have_value(prompt)
                    print("PASS partial automatic gate imports only three ready/warning candidates, rejects bad GLB/camera before allocation, keeps compact safe diagnostics and the current scene/draft/camera", flush=True)

                    page.set_viewport_size({"width": 340, "height": 800})
                    page.wait_for_function("document.querySelector('.sidebar-panel').getBoundingClientRect().width <= 316")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.querySelector('.sidebar-panel').scrollWidth <= document.querySelector('.sidebar-panel').clientWidth+1")
                    page.screenshot(path="/tmp/scene_feedback_ready_check.png")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    entry.fill(str(unknown))
                    with page.expect_response(lambda response: response.url.endswith("/api/projects/import-folder")) as empty:
                        page.locator("#folder-import-submit").click()
                    assert empty.value.json()["ready_check"]["status"] == "empty"
                    expect(page.locator("#folder-import-result")).to_contain_text("导入失败")
                    expect(page.locator("#folder-ready-summary")).to_contain_text("未识别到 ready 实例")
                    assert factory.call_count == 4 and len(registry.contexts()) == 5
                    assert not checks and not errors and not dialogs, (checks, errors, dialogs)
                    assert all(url.endswith("/api/projects/import-folder") or url.endswith("/api/projects/folders") for _, url in writes), writes
                    for loaded in registry.contexts():
                        assert loaded.gateway.adapter is None
                        assert loaded.store.workspace()["queue"] == [] and loaded.store.state["feedback"] == []
                    targets.assert_not_called()
                    models.assert_not_called()
                    print("PASS narrow layout, unknown-format error, no native approvals, separate checks, browser errors, feedback/model/task calls", flush=True)
                    browser.close()
            finally:
                server.shutdown()
                worker.join(5)
                server.server_close()
    print("ALL AUTOMATIC READY GATE BROWSER CHECKS PASSED", flush=True)


if __name__ == "__main__":
    main()
