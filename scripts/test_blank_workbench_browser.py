#!/usr/bin/env python3
"""Open a blank workbench, import scenes, and return to its empty landing page."""

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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts"), str(ROOT / "tests")]

from gateway import WorkspaceGateway
from start_blank_workbench import create_blank_server
from test_folder_import_browser import model_catalog, source_files
from test_prompt_drag_browser import image_data
from workspace_ui_helpers import control


def ready(page) -> None:
    page.wait_for_function("window.__blankCheck?.state.workspaceReady && "
                           "Number.isInteger(__blankCheck.state.sceneRevision) && !__blankCheck.state.sceneLoading")


def sidebar(page) -> None:
    if not page.locator("#projects-dialog").evaluate("el=>el.open"):
        page.locator("#projects-dialog-button").click()
    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase==='open'")


def import_folder(page, folder: Path, *, landing=False) -> dict:
    if landing:
        page.locator("#workspace-empty-open-folder").click()
    else:
        sidebar(page)
        page.locator("#sidebar-open-folder").click()
    page.wait_for_function("document.getElementById('projects-dialog').dataset.phase==='open' && "
                           "document.getElementById('projects-dialog').dataset.page==='import'")
    entry = page.locator("#folder-import-path")
    entry.fill(str(folder))
    with page.expect_response(lambda response: response.request.method=="POST" and
                              response.url.endswith("/api/projects/import-folder")) as response:
        entry.press("Enter")
    assert response.value.status == 200, response.value.text()
    result = response.value.json()
    page.locator('#sidebar-folder-import [data-sidebar-home]').click()
    page.wait_for_function("!__blankCheck.state.loadingProjects")
    return result


def row(page, project_id: str):
    return page.locator(f'.project-row[data-project-id="{project_id}"]')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    executable = Path("/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome")
    parser.add_argument("--browser-executable", default=str(executable) if executable.is_file() else None)
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="blank-workbench-browser-") as temporary:
        root = Path(temporary)
        project = root / "blank_project"
        project.mkdir()
        model_catalog(root / "catalog")
        original_sources = source_files(root / "catalog")
        errors, writes = [], []
        with patch.object(WorkspaceGateway, "create_target", side_effect=AssertionError("blank workbench created a task")) as targets, \
             patch.object(WorkspaceGateway, "list_models", side_effect=AssertionError("blank workbench requested models")) as models:
            server = create_blank_server(port=0, data_dir=root / "data", project_dir=project)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            store, gateway, registry = server.scene_store, server.workspace_gateway, server.project_registry
            session_id = gateway.ensure()["session_id"]
            base = f"http://127.0.0.1:{server.server_port}"
            root_scene = copy.deepcopy(store.scene())
            root_session = copy.deepcopy(store.get_session(session_id))
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True,
                        **({"executable_path": args.browser_executable} if args.browser_executable else {}),
                        args=["--no-sandbox", "--no-proxy-server", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    hook = "\nwindow.__blankCheck={state,camera,cameraData,controls,saveDraft,renderer};"
                    context.route("**/app.js", lambda route: route.fulfill(status=200, content_type="application/javascript",
                                  body=(ROOT / "web" / "app.js").read_text() + hook))
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: writes.append((request.method,urlsplit(request.url).path))
                            if request.method not in {"GET", "HEAD", "OPTIONS"} else None)
                    page.goto(base + '/')
                    ready(page)
                    assert page.url == base + '/'
                    expect(page.locator("#workspace-empty")).to_be_visible()
                    expect(page.locator("#workspace-empty-open-folder")).to_be_enabled()
                    expect(page.locator("#scene-stage")).to_be_hidden()
                    expect(page.locator("#reference-stage")).to_be_hidden()
                    expect(page.locator("#chat-dock")).to_be_hidden()
                    expect(page.locator("#chat-launcher")).to_be_hidden()
                    assert store.scene()["objects"] == [] and store.workspace()["thread_id"] is None
                    assert store.workspace()["created_thread_ids"] == [] and gateway.adapter is None
                    assert writes == []
                    page.screenshot(path="/tmp/scene_feedback_blank_workbench_053.png")

                    # The landing remains usable in narrow and immersive layouts.
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#workspace-empty-open-folder")).to_be_in_viewport()
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    control(page, "#immersive-toggle").click()
                    expect(page.locator("#workspace-empty-open-folder")).to_be_visible()
                    expect(page.locator("#scene-stage")).to_be_hidden()
                    control(page, "#comparison-layout-button").click()
                    print("PASS blank root shows a compact folder landing with no scene/chat/task; desktop, mobile and immersive entry remain usable", flush=True)

                    imported = import_folder(page, root / "catalog", landing=True)
                    assert len(imported["projects"]) == 2 and not imported["errors"]
                    first_id = imported["projects"][0]["project_id"]
                    expect(page.locator("#project-list .project-item")).to_have_count(3)
                    expect(row(page, registry.root.project_id).locator(".project-unload-trigger")).to_be_hidden()
                    assert store.scene() == root_scene and store.get_session(session_id) == root_session
                    row(page, first_id).locator(".project-item").click()
                    page.wait_for_url(re.compile(re.escape(base+"/p/"+first_id)+r"/.*"))
                    ready(page)
                    page.wait_for_function("__blankCheck.renderer.info.render.triangles>0 && "
                                           "[...__blankCheck.state.objectNodes.values()].some(node=>node.userData.gltfRoot)")
                    expect(page.locator("#workspace-empty")).to_be_hidden()
                    expect(page.locator("#scene-stage")).to_be_visible()
                    expect(page.locator("#reference-empty")).to_be_visible()
                    child = registry.get(first_id)
                    child_session_id = child.gateway.ensure()["session_id"]
                    if not page.locator("#feedback-note").is_visible():
                        control(page,"#chat-launcher").click()
                    child_draft = "场景重新打开后保留这条未发送提示。"
                    page.locator("#feedback-note").fill(child_draft)
                    page.evaluate("__blankCheck.camera.position.x+=.25;__blankCheck.controls.update();__blankCheck.saveDraft()")
                    child_camera = page.evaluate("JSON.stringify(__blankCheck.cameraData())")
                    child_scene = copy.deepcopy(child.store.scene())
                    sidebar(page)
                    row(page,first_id).locator(".project-unload-trigger").click()
                    row(page,first_id).locator(".project-unload-confirm").click()
                    page.wait_for_url(base + '/')
                    ready(page)
                    expect(page.locator("#workspace-empty")).to_be_visible()
                    expect(page.locator("#chat-dock")).to_be_hidden()
                    expect(page.locator("#scene-stage")).to_be_hidden()
                    assert store.scene() == root_scene and store.get_session(session_id) == root_session
                    print("PASS landing folder button imports two real GLBs; opening enters the normal scene; unloading current returns to the unchanged blank root", flush=True)

                    restored = import_folder(page,root / "catalog",landing=True)
                    assert {item["project_id"] for item in restored["projects"]} == {item["project_id"] for item in imported["projects"]}
                    restored_child = registry.get(first_id)
                    assert restored_child.gateway.ensure()["session_id"] == child_session_id
                    assert restored_child.store.scene() == child_scene
                    row(page,first_id).locator(".project-item").click()
                    page.wait_for_url(re.compile(re.escape(base+"/p/"+first_id)+r"/.*"))
                    ready(page)
                    expect(page.locator("#workspace-empty")).to_be_hidden()
                    expect(page.locator("#feedback-note")).to_have_value(child_draft)
                    assert page.evaluate("JSON.stringify(__blankCheck.cameraData())") == child_camera
                    assert source_files(root / "catalog") == original_sources

                    # A reference-only workspace is a valid normal workspace.
                    store.add_reference(session_id,"reference.png",image_data("navy"))
                    page.goto(server.browser_url(session_id))
                    ready(page)
                    page.wait_for_function("document.getElementById('reference-image').naturalWidth>0")
                    expect(page.locator("#workspace-empty")).to_be_hidden()
                    expect(page.locator("#reference-media")).to_be_visible()
                    expect(page.locator("#scene-stage")).to_be_visible()
                    assert page.evaluate("__blankCheck.state.sceneObjects.length") == 0
                    targets.assert_not_called()
                    models.assert_not_called()
                    assert all(path.endswith("/api/projects/folders") or path.endswith("/api/projects/import-folder") or
                               method=="DELETE" and "/api/projects/" in path for method,path in writes), writes
                    assert store.state["feedback"] == [] and not errors, errors
                    print("PASS reimport restores scene/session/draft/camera; reference-only workspaces retain their normal view; sources preserved and no model, task or feedback calls",flush=True)
                    browser.close()
            finally:
                server.shutdown()
                worker.join(5)
                server.server_close()
    print("ALL BLANK WORKBENCH BROWSER CHECKS PASSED",flush=True)


if __name__ == "__main__":
    main()
