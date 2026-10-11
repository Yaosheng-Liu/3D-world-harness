#!/usr/bin/env python3
"""Unload and restore imported scenes through an isolated HTTP workbench."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts"), str(ROOT / "tests")]

from gateway import WorkspaceGateway
from server import make_server
from test_folder_import_browser import model_catalog, png, source_files
from test_object_double_click_browser import model
from workspace_ui_helpers import control


def request(base: str, path: str, *, method="GET", capability=None) -> tuple[int, dict]:
    headers = {"X-Workspace-Capability": capability} if capability else {}
    call = Request(base + path, method=method, headers=headers)
    try:
        response = build_opener(ProxyHandler({})).open(call, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def ready(page) -> None:
    page.wait_for_function("window.__unloadCheck?.state.workspaceReady && !__unloadCheck.state.sceneLoading && "
                           "!__unloadCheck.state.seeking && __unloadCheck.renderer.info.render.triangles > 0")


def sidebar(page) -> None:
    if not page.locator("#projects-dialog").evaluate("el=>el.open"):
        page.locator("#projects-dialog-button").click()
    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase === 'open'")


def import_folder(page, path: Path) -> dict:
    sidebar(page)
    page.locator("#sidebar-open-folder").click()
    entry = page.locator("#folder-import-path")
    entry.fill(str(path))
    with page.expect_response(lambda response: response.request.method == "POST" and
                              response.url.endswith("/api/projects/import-folder")) as response:
        entry.press("Enter")
    assert response.value.status == 200, (response.value.status,response.value.text())
    result = response.value.json()
    page.locator('#sidebar-folder-import [data-sidebar-home]').click()
    page.wait_for_function("!__unloadCheck.state.loadingProjects")
    return result


def row(page, project_id: str):
    return page.locator(f'.project-row[data-project-id="{project_id}"]')


def draft(page, text: str) -> None:
    if not page.locator("#feedback-note").is_visible():
        control(page, "#chat-launcher").click()
    page.locator("#feedback-note").fill(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    executable = Path("/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome")
    parser.add_argument("--browser-executable", default=str(executable) if executable.is_file() else None)
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="scene-unload-browser-") as temporary:
        root = Path(temporary)
        original = root / "original"
        original.mkdir()
        model(original / "original.glb")
        png(original / "original.png", "gray")
        sources = model_catalog(root / "catalog")
        source_before = source_files(root / "catalog")
        errors, deletes, writes = [], [], []
        with patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("unload created a task")) as targets, \
             patch.object(WorkspaceGateway, "list_models", side_effect=AssertionError("unload requested models")) as models:
            server = make_server(port=0, data_dir=root / "data", project_dir=original,
                                 web_dir=ROOT / "web", external_review=True, feedback_transport="mcp_events")
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            store, gateway, registry = server.scene_store, server.workspace_gateway, server.project_registry
            session_id = gateway.ensure()["session_id"]
            store.import_model(str(original / "original.glb"), object_id="original_model", name="原始模型")
            gateway.add_reference_paths([str(original / "original.png")])
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True,
                        **({"executable_path": args.browser_executable} if args.browser_executable else {}),
                        args=["--no-sandbox", "--no-proxy-server", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    hook = "\nwindow.__unloadCheck={state,cameraData,renderer,loadProjects};"
                    context.route("**/app.js", lambda route: route.fulfill(status=200, content_type="application/javascript",
                                  body=(ROOT / "web" / "app.js").read_text() + hook))
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda call: deletes.append(call.url) if call.method == "DELETE" else None)
                    page.on("request", lambda call: writes.append((call.method, call.url))
                            if call.method not in {"GET", "HEAD", "OPTIONS"} else None)
                    page.goto(server.browser_url(session_id))
                    ready(page)
                    root_draft = "默认场景的未发送草稿。"
                    draft(page, root_draft)
                    root_scene = copy.deepcopy(store.scene())
                    root_session = copy.deepcopy(store.get_session(session_id))
                    initial_url = page.url
                    camera_before = page.evaluate("JSON.stringify(__unloadCheck.cameraData())")
                    imported = import_folder(page, root / "catalog")
                    assert len(imported["projects"]) == 2 and not imported["errors"]
                    first, second = imported["projects"]
                    first_id, second_id = first["project_id"], second["project_id"]
                    expect(page.locator("#project-list .project-item")).to_have_count(3)
                    expect(row(page, registry.root.project_id).locator(".project-unload-trigger")).to_be_hidden()
                    expect(row(page, second_id).locator(".project-unload-trigger")).to_be_visible()
                    root_capability = page.evaluate("__unloadCheck.state.browserCapability")
                    status, _ = request(base, f"/api/projects/{second_id}", method="DELETE")
                    assert status == 403
                    status, body = request(base, f"/api/projects/{registry.root.project_id}", method="DELETE", capability=root_capability)
                    assert status == 409 and "默认场景" in body["error"]

                    # Opening, refreshing and cancelling are local operations.
                    second_row = row(page, second_id)
                    second_row.locator(".project-unload-trigger").click()
                    expect(second_row.locator(".project-unload-panel")).to_be_visible()
                    expect(second_row.locator(".project-unload-description")).to_contain_text("源文件、场景数据和草稿保留")
                    page.screenshot(path="/tmp/scene_unload_052.png")
                    cancel = second_row.locator(".project-unload-cancel").element_handle()
                    page.evaluate("__unloadCheck.loadProjects()")
                    assert cancel.evaluate("el=>el.isConnected && document.activeElement===el")
                    second_row.locator(".project-unload-cancel").click()
                    expect(second_row.locator(".project-unload-panel")).to_be_hidden()
                    assert not deletes and page.url == initial_url
                    print("PASS root protected, unauthenticated delete denied; compact confirmation survives refresh/focus and cancels without navigation", flush=True)

                    # A failed request keeps the row, confirmation and draft.
                    failure_calls = []
                    second_delete = re.compile(r"/api/projects/" + second_id + r"$")

                    def deny_once(route):
                        if route.request.method != "DELETE":
                            route.continue_()
                        else:
                            failure_calls.append(route.request.url)
                            route.fulfill(status=409, content_type="application/json",
                                          body=json.dumps({"error": "场景正在处理反馈，请稍后重试。"}, ensure_ascii=False))

                    page.route(second_delete, deny_once)
                    second_row.locator(".project-unload-trigger").click()
                    second_row.locator(".project-unload-confirm").click()
                    expect(second_row.locator(".project-unload-error")).to_contain_text("请稍后重试")
                    expect(second_row.locator(".project-unload-confirm")).to_be_enabled()
                    assert len(failure_calls) == 1 and page.url == initial_url
                    expect(page.locator("#feedback-note")).to_have_value(root_draft)
                    page.unroute(second_delete, deny_once)

                    second_context = registry.get(second_id)
                    second_session_id = second_context.gateway.ensure()["session_id"]
                    second_bytes = source_files(second_context.store.data_dir)
                    held = []

                    def hold_delete(route):
                        if route.request.method == "DELETE":
                            response = route.fetch()
                            assert response.status == 200
                            held.append((route, response))
                        else:
                            route.continue_()

                    page.route(second_delete, hold_delete)
                    second_row.locator(".project-unload-confirm").click()
                    expect(second_row.locator(".project-unload-confirm")).to_be_disabled()
                    second_row.locator(".project-unload-confirm").evaluate("el=>{el.dispatchEvent(new MouseEvent('click',{bubbles:true}));el.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
                    assert len(held) == 1 and len(deletes) == 2
                    held[0][0].fulfill(response=held[0][1])
                    expect(row(page, second_id)).to_have_count(0)
                    page.unroute(second_delete, hold_delete)
                    assert page.url == initial_url
                    expect(page.locator("#feedback-note")).to_have_value(root_draft)
                    assert page.evaluate("JSON.stringify(__unloadCheck.cameraData())") == camera_before
                    assert source_files(second_context.store.data_dir) == second_bytes
                    assert source_files(root / "catalog") == source_before
                    status, missing = request(base, f"/p/{second_id}/api/workspace/state")
                    assert status == 404
                    print("PASS 409 stays in place with retry; noncurrent unload sends once, removes only its row, preserves camera, draft and all data bytes", flush=True)

                    restored = import_folder(page, root / "catalog")
                    assert {item["project_id"] for item in restored["projects"]} == {first_id, second_id}
                    restored_second = registry.get(second_id)
                    assert restored_second.gateway.ensure()["session_id"] == second_session_id
                    assert source_files(restored_second.store.data_dir) == second_bytes
                    expect(page.locator("#project-list .project-item")).to_have_count(3)

                    # Current-scene unload must save its draft and return to the
                    # original root even if the first successful reply is lost.
                    row(page, first_id).locator(".project-item").click()
                    page.wait_for_url(re.compile(re.escape(base + "/p/" + first_id) + r"/.*"))
                    ready(page)
                    child_draft = "这个场景的草稿在卸载后也需要保留。"
                    draft(page, child_draft)
                    current_context = registry.get(first_id)
                    current_session_id = current_context.gateway.ensure()["session_id"]
                    current_bytes = source_files(current_context.store.data_dir)
                    current_url = page.url
                    sidebar(page)
                    current_row = row(page, first_id)
                    current_row.locator(".project-unload-trigger").click()
                    first_delete = re.compile(r"/api/projects/" + first_id + r"$")
                    lost = []

                    def lose_reply(route):
                        if route.request.method != "DELETE":
                            route.continue_()
                        else:
                            response = route.fetch()
                            assert response.status == 200
                            lost.append(response.json())
                            route.abort("failed")

                    page.route(first_delete, lose_reply)
                    current_row.locator(".project-unload-confirm").click()
                    expect(current_row.locator(".project-unload-error")).to_contain_text("卸载未完成")
                    expect(current_row.locator(".project-unload-confirm")).to_be_enabled()
                    assert len(lost) == 1 and page.url == current_url
                    expect(page.locator("#feedback-note")).to_have_value(child_draft)
                    saved = page.evaluate("JSON.parse(localStorage.getItem('astra-visual-draft:'+__unloadCheck.state.sessionId)).note")
                    assert saved == child_draft
                    assert source_files(current_context.store.data_dir) == current_bytes
                    page.unroute(first_delete, lose_reply)
                    current_row.locator(".project-unload-confirm").click()
                    page.wait_for_url(re.compile(re.escape(base + "/p/" + registry.root.project_id) + r"/.*"))
                    ready(page)
                    expect(page.locator("#feedback-note")).to_have_value(root_draft)
                    assert page.evaluate("__unloadCheck.state.sessionId") == session_id
                    assert store.scene() == root_scene and store.get_session(session_id) == root_session
                    assert source_files(root / "catalog") == source_before
                    restored = import_folder(page, root / "catalog")
                    assert {item["project_id"] for item in restored["projects"]} == {first_id, second_id}
                    restored_current = registry.get(first_id)
                    assert restored_current.gateway.ensure()["session_id"] == current_session_id
                    assert source_files(restored_current.store.data_dir) == current_bytes
                    row(page, first_id).locator(".project-item").click()
                    page.wait_for_url(re.compile(re.escape(base + "/p/" + first_id) + r"/.*"))
                    ready(page)
                    expect(page.locator("#feedback-note")).to_have_value(child_draft)
                    assert source_files(root / "catalog") == source_before
                    print("PASS current unload saves draft, retries a lost success via its original credential, returns to root; reimport restores the same session/model/draft", flush=True)

                    assert not errors, errors
                    assert all(method == "DELETE" or url.endswith("/api/projects/import-folder") or url.endswith("/api/projects/folders")
                               for method, url in writes), writes
                    for loaded in registry.contexts():
                        workspace = loaded.store.workspace()
                        assert workspace["queue"] == [] and loaded.store.state["feedback"] == []
                        assert loaded.gateway.adapter is None
                    targets.assert_not_called()
                    models.assert_not_called()
                    print("PASS no browser errors, feedback submissions, model calls or new tasks", flush=True)
                    browser.close()
            finally:
                server.shutdown()
                worker.join(5)
                server.server_close()
    print("ALL SCENE UNLOAD BROWSER CHECKS PASSED", flush=True)


if __name__ == "__main__":
    main()
