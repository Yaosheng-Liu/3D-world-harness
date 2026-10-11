#!/usr/bin/env python3
"""Check real chat-header drags against a temporary workbench and local fixtures."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]
from server import make_server
from test_object_double_click_browser import model
from test_prompt_drag_browser import image_data


def geometry(page):
    return page.evaluate("""()=>{
      const dock=document.querySelector('#chat-dock'),header=document.querySelector('#chat-dock-move');
      const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,top:r.top,width:r.width,height:r.height,right:r.right,bottom:r.bottom}};
      const v=visualViewport;
      return {dock:rect(dock),header:rect(header),viewport:{left:v?.offsetLeft||0,top:v?.offsetTop||0,
        width:v?.width||innerWidth,height:v?.height||innerHeight},
        maximum:Number(document.querySelector('#chat-resize-handle').getAttribute('aria-valuemax')),
        cssMaximum:parseFloat(getComputedStyle(dock).maxHeight)};
    }""")


def settle(page):
    page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")


def hold_expanded(page, *, history=False):
    # A real Tab establishes keyboard use; focus returns to the draggable header
    # without asking Chromium to scroll a partly clipped control into view.
    page.keyboard.press("Tab")
    page.locator("#chat-dock-move").evaluate("el=>el.focus({preventScroll:true})")
    page.wait_for_function("document.querySelector('#chat-dock').classList.contains('is-expanded')")
    if history and page.locator("#chat-history-toggle").get_attribute("aria-expanded") != "true":
        page.locator("#chat-history-toggle").click()
        page.keyboard.press("Tab")
        page.locator("#chat-dock-move").evaluate("el=>el.focus({preventScroll:true})")
    settle(page)


def header_point(page):
    point = page.evaluate("""()=>{
      const header=document.querySelector('#chat-dock-move'),r=header.getBoundingClientRect();
      const left=Math.max(1,r.left+3),right=Math.min(innerWidth-2,r.right-3);
      const top=Math.max(1,r.top+2),bottom=Math.min(innerHeight-2,r.bottom-2);
      // The component starts a drag only when the header itself is the target.
      // Scan its actually visible background, including when the grip is clipped.
      for(let y=(top+bottom)/2;y<bottom;y+=3)for(let x=left;x<right;x+=4)
        if(document.elementFromPoint(x,y)===header)return {x,y};
      return null;
    }""")
    assert point, ("No visible draggable header background", geometry(page))
    return point


def drag_to(page, left, top, *, cancel=False):
    start = header_point(page)
    before = geometry(page)["dock"]
    page.mouse.move(start["x"], start["y"])
    page.mouse.down()
    assert page.locator("#chat-dock").evaluate("el=>el.classList.contains('moving')")
    page.mouse.move(start["x"] + left - before["left"], start["y"] + top - before["top"], steps=10)
    during = geometry(page)
    if cancel:
        page.keyboard.press("Escape")
        assert not page.locator("#chat-dock").evaluate("el=>el.classList.contains('moving')")
    page.mouse.up()
    hold_expanded(page)
    return during, geometry(page)


def limits(data):
    dock, header, viewport = data["dock"], data["header"], data["viewport"]
    horizontal = min(160, dock["width"] * .25)
    vertical = min(96, dock["height"] * .25)
    top = min(16, max(0, header["height"] - 24))
    return {"left": viewport["left"] - horizontal,
            "right": viewport["left"] + viewport["width"] - dock["width"] + horizontal,
            "top": viewport["top"] - top,
            "bottom": viewport["top"] + viewport["height"] - dock["height"] + vertical}


def near(actual, expected, label):
    assert abs(actual - expected) <= 1.1, (label, actual, expected)


def assert_reachable(data):
    header, viewport = data["header"], data["viewport"]
    visible_height = min(header["bottom"], viewport["top"] + viewport["height"]) - max(header["top"], viewport["top"])
    assert visible_height >= 23.9, ("At least 24px of the real header remains visible", data)
    assert header["right"] > viewport["left"] and header["left"] < viewport["left"] + viewport["width"], data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", default="/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="chat-dock-overhang-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        server = make_server(port=0, data_dir=root / "data", project_dir=project, web_dir=ROOT / "web",
                             external_review=True, feedback_transport="mcp_events")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = server.workspace_gateway.ensure()["session_id"]
        asset = root / "fixture.glb"
        model(asset)
        server.scene_store.import_model(str(asset), object_id="fixture_model", name="Fixture")
        server.scene_store.set_reference_clip(session, {"name": "Fixture", "fps": 1,
            "frames": [{"name": "frame.png", "data_url": image_data("navy"), "time_sec": 0}]})
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser_executable,
                    args=["--no-sandbox", "--no-proxy-server", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
                context = browser.new_context(viewport={"width": 1100, "height": 900}, reduced_motion="reduce")
                hook = "\nwindow.__chatOverhang={state};"
                context.route("**/app.js", lambda route: route.fulfill(status=200, content_type="application/javascript",
                    body=(ROOT / "web/app.js").read_text() + hook))
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                url = f"http://127.0.0.1:{server.server_port}/"

                def ready():
                    page.wait_for_function("window.__chatOverhang && __chatOverhang.state.workspaceReady && !__chatOverhang.state.sceneLoading && !__chatOverhang.state.seeking && document.querySelector('#reference-image').naturalWidth>0")

                def preference():
                    return page.evaluate("key=>JSON.parse(localStorage.getItem(key))", "astra-visual-layout:" + session)

                page.goto(url)
                ready()
                hold_expanded(page, history=True)
                page.locator("#chat-resize-handle").press("End")
                for _ in range(4):
                    page.locator("#chat-resize-handle").press("Shift+ArrowDown")
                hold_expanded(page)
                default = geometry(page)["dock"]
                assert not page.locator("#chat-dock").evaluate("el=>el.classList.contains('is-positioned')")
                drag_to(page, 140, 130)
                baseline = geometry(page)
                assert baseline["dock"]["height"] > 300, baseline

                targets = [("left", -2000, 130), ("right", 2500, 130),
                           ("top", 140, -2000), ("bottom", 140, 2500)]
                for direction, left, top in targets:
                    during, after = drag_to(page, left, top)
                    axis = "left" if direction in {"left", "right"} else "top"
                    near(after["dock"][axis], limits(after)[direction], direction + " boundary")
                    for data in (during, after):
                        near(data["dock"]["height"], baseline["dock"]["height"], direction + " unchanged height")
                        near(data["cssMaximum"], baseline["cssMaximum"], direction + " unchanged CSS height budget")
                        assert_reachable(data)
                    near(after["maximum"], baseline["maximum"], direction + " unchanged history height budget")
                    drag_to(page, 140, 130)
                    near(geometry(page)["dock"]["left"], 140, direction + " drag back left")
                    near(geometry(page)["dock"]["top"], 130, direction + " drag back top")
                print("PASS: real drags reach all four overhang limits, retain 24px of header and preserve height/budgets; every visible header drags back", flush=True)

                drag_to(page, 2500, 180)
                preferred = geometry(page)["dock"]
                saved = preference()
                page.reload()
                ready()
                hold_expanded(page, history=True)
                near(geometry(page)["dock"]["left"], preferred["left"], "refresh restores preferred left")
                near(geometry(page)["dock"]["top"], preferred["top"], "refresh restores preferred top")
                assert preference()["dockPositions"] == saved["dockPositions"]
                page.set_viewport_size({"width": 650, "height": 500})
                settle(page)
                assert_reachable(geometry(page))
                assert preference()["dockPositions"] == saved["dockPositions"], "Temporary viewport clamp must not overwrite position preference"
                assert preference()["dockHeight"] == saved["dockHeight"], "Temporary height clamp must not overwrite height preference"
                page.set_viewport_size({"width": 1100, "height": 900})
                hold_expanded(page, history=True)
                restored = geometry(page)["dock"]
                for axis in ("left", "top", "height"):
                    near(restored[axis], preferred[axis], "viewport restoration " + axis)
                print("PASS: refresh preserves preference; a smaller viewport keeps the header reachable and restoration recovers the preferred position and height", flush=True)

                # Compact/expanded sizes have their own temporary bounds.
                page.locator("#chat-dock-move").evaluate("el=>el.blur()")
                page.mouse.move(5, 500)
                page.wait_for_function("document.querySelector('#chat-dock').classList.contains('is-compact')")
                assert_reachable(geometry(page))
                assert preference()["dockPositions"] == saved["dockPositions"]
                hold_expanded(page, history=True)
                near(geometry(page)["dock"]["left"], preferred["left"], "expanded restores preferred left")
                near(geometry(page)["dock"]["top"], preferred["top"], "expanded restores preferred top")
                before_cancel = geometry(page)["dock"]
                before_preference = preference()["dockPositions"]
                drag_to(page, 40, 250, cancel=True)
                for axis in ("left", "top"):
                    near(geometry(page)["dock"][axis], before_cancel[axis], "Escape restores " + axis)
                assert preference()["dockPositions"] == before_preference
                page.locator("#chat-dock-move").press("ArrowLeft")
                near(geometry(page)["dock"]["left"], before_cancel["left"] - 8, "keyboard nudge")
                page.locator("#chat-dock-move").press("Home")
                assert not page.locator("#chat-dock").evaluate("el=>el.classList.contains('is-positioned')")
                for axis in ("left", "top"):
                    near(geometry(page)["dock"][axis], default[axis], "Home returns default " + axis)
                drag_to(page, 120, 140)
                point = header_point(page)
                page.mouse.dblclick(point["x"], point["y"], delay=70)
                hold_expanded(page)
                assert not page.locator("#chat-dock").evaluate("el=>el.classList.contains('is-positioned')")
                for axis in ("left", "top"):
                    near(geometry(page)["dock"][axis], default[axis], "double-click returns default " + axis)
                assert not preference()["dockPositions"]
                assert not errors, errors
                print("PASS: compact/expanded remains reachable without saving clamps; Escape cancels, arrows nudge, and Home/double-click restore the unchanged default", flush=True)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
