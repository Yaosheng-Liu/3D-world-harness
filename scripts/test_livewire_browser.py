#!/usr/bin/env python3
"""Exercise edge-following reference contours in an isolated real workbench."""
from __future__ import annotations

import argparse
import base64
import copy
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import threading

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'scripts'), str(ROOT / 'tests')]
from server import make_server
from test_object_double_click_browser import model
from workspace_ui_helpers import choose_tool, control, open_annotation_tools


def reference_image(second=False):
    image = Image.new('RGB', (320, 240), '#20252c')
    ImageDraw.Draw(image).rectangle((64, 48, 240, 192), fill='#ead6ba' if second else '#f7f7f7')
    stream = BytesIO(); image.save(stream, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()


def ready(page):
    page.wait_for_function('window.__livewire && __livewire.state.workspaceReady && '
        '!__livewire.state.sceneLoading && !__livewire.state.seeking && '
        '!__livewire.state.submitting && document.getElementById("reference-image").naturalWidth>0')


def idle(page):
    page.wait_for_function('__livewire.livewire.active && !__livewire.livewire.busy')


def point(page, x, y):
    rect = page.locator('#reference-annotations').bounding_box(); assert rect
    return {'x': rect['x'] + rect['width'] * x, 'y': rect['y'] + rect['height'] * y}


def anchor(page, x, y):
    page.mouse.click(**point(page, x, y)); idle(page)


def marks(page):
    return page.evaluate('structuredClone(__livewire.state.annotations)')


def near(p, q, tolerance=.012):
    return max(abs(p[key] - q[key]) for key in ('x', 'y')) < tolerance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable', default='/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='livewire-browser-') as temporary:
        root = Path(temporary); project = root / 'project'; project.mkdir()
        server = make_server(port=0, data_dir=root / 'data', project_dir=project,
            web_dir=ROOT / 'web', external_review=True, feedback_transport='mcp_events')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        store = server.scene_store; session = server.workspace_gateway.ensure()['session_id']
        asset = root / 'model.glb'; model(asset)
        store.import_model(str(asset), object_id='fixture_model', name='Preview model')
        camera = {'camera_to_world': [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]],
            'intrinsics': {'width': 320, 'height': 240, 'fx': 300, 'fy': 300, 'cx': 160, 'cy': 120}}
        original = reference_image()
        store.set_reference_clip(session, {'name': 'Contour fixture', 'fps': 2, 'frames': [
            {'name': 'frame0.png', 'data_url': original, 'time_sec': 0, 'camera': camera},
            {'name': 'frame1.png', 'data_url': reference_image(True), 'time_sec': .5, 'camera': camera}]})
        scene = copy.deepcopy(store.scene()); errors = []; posted = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, executable_path=args.browser_executable,
                    args=['--no-sandbox', '--no-proxy-server', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, device_scale_factor=2)
                hook = '\nwindow.__livewire={state,ui,livewire,setMode,setReferenceZoom,promptText,cameraData};'
                context.route('**/app.js', lambda route: route.fulfill(status=200, content_type='application/javascript',
                    body=(ROOT / 'web/app.js').read_text() + hook))
                held_worker = []; worker_gate = {'hold': True}
                def worker_route(route):
                    if worker_gate['hold']: held_worker.append(route)
                    else: route.continue_()
                context.route('**/livewire-worker.js', worker_route)
                page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda call: posted.append(call.post_data_json)
                    if call.method == 'POST' and call.url.endswith('/feedback') else None)
                page.goto(server.browser_url(session)); ready(page)
                if page.locator('#chat-dock').is_visible(): control(page, '#chat-collapse').click()
                choose_tool(page, 'livewire', 'reference')
                assert page.locator('button[data-tool="livewire"]').is_visible()
                source_id = page.evaluate('__livewire.state.activeReferenceId')
                source_pixels = page.locator('#reference-image').get_attribute('src')

                # A stalled worker download must not block the UI or turn a
                # cancelled preparation into persisted feedback afterwards.
                page.mouse.click(**point(page, .2, .2))
                page.wait_for_function('__livewire.livewire.active && __livewire.livewire.busy')
                assert len(held_worker) == 1
                page.keyboard.press('Escape'); page.wait_for_function('!__livewire.livewire.active')
                assert marks(page) == []
                worker_gate['hold'] = False
                held_worker.pop().fulfill(status=200, content_type='application/javascript', body=(ROOT / 'web/livewire-worker.js').read_text())
                page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                assert not page.evaluate('__livewire.livewire.active') and marks(page) == []
                print('PASS: Escape remains responsive while the worker download is stalled; releasing the old request cannot resurrect a cancelled contour or feedback', flush=True)

                # Opposite corners require following two object boundaries, not
                # the straight chord through its uniform interior. Real pointer
                # movement must preview the same edge-following evidence.
                anchor(page, .2, .2)
                page.mouse.move(**point(page, .75, .8))
                page.wait_for_function('__livewire.livewire.previewPoints.length>2 && !__livewire.livewire.busy')
                preview = page.evaluate('structuredClone(__livewire.livewire.previewPoints)')
                anchor(page, .75, .8)
                page.keyboard.press('Enter')
                page.wait_for_function('__livewire.state.annotations.length===1 && !__livewire.livewire.active')
                opened = marks(page)[0]
                assert opened['type'] == 'freehand' and opened['pane'] == 'reference'
                assert opened['reference_image_id'] == source_id and opened['name'].startswith('智能轮廓')
                assert 3 <= len(opened['points']) <= 256
                assert near(opened['points'][0], {'x': .2, 'y': .2})
                assert near(opened['points'][-1], {'x': .75, 'y': .8})
                for path in (preview, opened['points']):
                    boundary = [min(abs(p['x']-.2), abs(p['x']-.75), abs(p['y']-.2), abs(p['y']-.8)) for p in path]
                    assert sum(distance < .035 for distance in boundary) / len(boundary) > .9, path
                    assert any(abs((p['y']-.2)-(.6/.55)*(p['x']-.2)) > .15 for p in path), 'path is a straight chord'
                assert page.locator('#reference-image').get_attribute('src') == source_pixels
                print('PASS: real pointer preview and Enter trace strong image boundaries rather than a straight chord; the result uses normalized original-image coordinates at DPR 2', flush=True)

                # Source coordinates remain image-relative when the display is
                # zoomed. Keyboard removal/cancel must leave earlier evidence
                # intact; clicking the first anchor produces a closed contour.
                center = point(page, .5, .5); page.mouse.move(**center); page.mouse.wheel(0, -300)
                page.wait_for_function('__livewire.state.referenceZoom>1')
                choose_tool(page, 'livewire', 'reference')
                anchor(page, .2, .2); anchor(page, .75, .2); anchor(page, .75, .8)
                page.keyboard.press('Backspace')
                page.wait_for_function('__livewire.livewire.anchors.length===2 && !__livewire.livewire.busy')
                assert marks(page) == [opened]
                page.keyboard.press('Escape')
                page.wait_for_function('!__livewire.livewire.active'); assert marks(page) == [opened]
                anchor(page, .2, .2); anchor(page, .75, .2); anchor(page, .75, .8); anchor(page, .2, .8)
                page.mouse.click(**point(page, .2, .2))
                page.wait_for_function('__livewire.state.annotations.length===2 && !__livewire.livewire.active')
                closed = marks(page)[1]
                assert near(closed['points'][0], {'x': .2, 'y': .2}) and near(closed['points'][-1], closed['points'][0])
                assert all(0 <= p[k] <= 1 for p in closed['points'] for k in ('x', 'y')) and len(closed['points']) <= 256
                control(page, '#undo-annotation').click(); assert marks(page) == [opened]
                control(page, '#redo-annotation').click(); assert marks(page) == [opened, closed]
                anchor(page, .2, .3)
                page.mouse.dblclick(**point(page, .2, .65), delay=70)
                page.wait_for_function('__livewire.state.annotations.length===3 && !__livewire.livewire.active')
                double_clicked = marks(page)[2]
                assert near(double_clicked['points'][0], {'x': .2, 'y': .3})
                assert near(double_clicked['points'][-1], {'x': .2, 'y': .65})
                control(page, '#undo-annotation').click(); assert marks(page) == [opened, closed]
                choose_tool(page, 'select', 'reference')
                page.mouse.dblclick(**point(page, .75, .5), delay=70)
                page.wait_for_function('id=>__livewire.promptText().includes("[[annotation:"+id+"]]")', arg=closed['id'])
                assert '📍' in page.locator('#feedback-note').input_value()
                assert marks(page) == [opened, closed]
                print('PASS: zoomed anchors retain source coordinates; Backspace and Escape change only a temporary path, closure and real double-click completion are bounded, undo/redo and double-click citation retain the completed contour', flush=True)

                # Beginning a contour on frame 0 and moving the timeline must
                # cancel its temporary state. It must never reappear on frame 1
                # when an older asynchronous worker response completes.
                page.locator('#feedback-note').fill('')
                if page.locator('#chat-dock').is_visible(): control(page, '#chat-collapse').click()
                choose_tool(page, 'livewire', 'reference'); anchor(page, .3, .2)
                page.locator('#chat-launcher').click()
                page.locator('#feedback-note').fill('保留尚未完成的轮廓草稿')
                anchors_before = page.evaluate('structuredClone(__livewire.livewire.anchors)')
                page.locator('#submit-button').click()
                page.wait_for_function('__livewire.ui.toast.textContent.includes("智能轮廓")')
                assert posted == [] and store.state['feedback'] == []
                assert page.evaluate('structuredClone(__livewire.livewire.anchors)') == anchors_before
                assert page.locator('#feedback-note').input_value() == '保留尚未完成的轮廓草稿'
                assert marks(page) == [opened, closed] and page.evaluate('__livewire.livewire.active')
                page.locator('#feedback-note').fill(''); control(page, '#chat-collapse').click()
                # Dispatch the range's native input flow at 0.1 s. With one
                # reference view the displayed clock snaps to frame 0, so this
                # specifically checks cancellation when the GT identity stays.
                page.locator('#timeline-seek').evaluate('(el,value)=>{el.value=String(value);el.dispatchEvent(new Event("input",{bubbles:true}));}', .1)
                page.wait_for_function('!__livewire.livewire.active && !__livewire.state.seeking && __livewire.state.timelineTarget===null && __livewire.state.scrubRequest===null')
                assert page.evaluate('__livewire.state.activeReferenceId') == source_id
                assert page.locator('#reference-image').get_attribute('src') == source_pixels
                assert marks(page) == [opened, closed]
                page.locator('#timeline-seek').evaluate('(el,value)=>{el.value=String(value);el.dispatchEvent(new Event("input",{bubbles:true}));}', 0)
                page.wait_for_function('__livewire.state.time===0 && !__livewire.state.seeking && __livewire.state.timelineTarget===null && __livewire.state.scrubRequest===null')
                anchor(page, .3, .2)
                page.mouse.move(**point(page, .75, .65))
                page.locator('#timeline-next').click()
                page.wait_for_function('__livewire.state.time===.5 && !__livewire.state.seeking && !__livewire.livewire.active')
                page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                assert marks(page) == [opened, closed]
                open_annotation_tools(page, 'scene')
                assert not page.locator('button[data-tool="livewire"]').is_visible(), 'Livewire must be a reference-only tool'
                assert page.evaluate('__livewire.state.annotations.filter(a=>a.pane==="scene").length') == 0
                page.locator('#timeline-prev').click(); ready(page)
                page.wait_for_function('__livewire.state.time===0 && !__livewire.state.seeking')
                page.reload(); ready(page)
                assert marks(page) == [opened, closed], 'completed contours must survive a draft reload'
                assert not page.evaluate('__livewire.livewire.active'), 'temporary path must not survive reload'
                choose_tool(page, 'livewire', 'reference'); anchor(page, .3, .2)
                moments_before_clear = page.evaluate('structuredClone(__livewire.state.dynamicSnapshots)')
                assert moments_before_clear
                # The sidebar's round clear removes both marks and saved
                # moments. Its confirmation must also cancel the pending path;
                # one undo restores complete evidence without reviving it.
                control(page, '#clear-round').click()
                page.locator('#confirm-clear-annotations').click()
                page.wait_for_function('!__livewire.livewire.active && __livewire.state.annotations.length===0 && __livewire.state.dynamicSnapshots.length===0')
                # Native dialog close queues the sidebar's animated close.
                # Wait for it, rather than toggling its close button during the
                # transition and accidentally reopening the drawer.
                page.wait_for_function('!document.getElementById("projects-dialog").open')
                control(page, '#undo-annotation').click(); ready(page)
                assert marks(page) == [opened, closed]
                assert page.evaluate('structuredClone(__livewire.state.dynamicSnapshots)') == moments_before_clear
                assert not page.evaluate('__livewire.livewire.active')
                choose_tool(page, 'select', 'reference')
                page.mouse.dblclick(**point(page, .75, .5), delay=70)
                page.wait_for_function('id=>__livewire.promptText().includes("[[annotation:"+id+"]]")', arg=closed['id'])
                with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/feedback')) as response:
                    page.locator('#submit-button').click()
                assert response.value.status == 201, response.value.text()
                saved = response.value.json()
                assert len(posted) == 1 and len(saved['annotations']) == 2
                for retained, expected in zip(saved['annotations'], (opened, closed)):
                    assert all(retained[key] == value for key, value in expected.items()), (retained, expected)
                assert saved['annotations'][0]['type'] == 'freehand' and 'livewire' not in saved['annotations'][0]
                source = next(frame for frame in saved['dynamic_frames'] if frame['reference_frame_id'] == source_id)
                source_file = store.media_dir / Path(source['reference_original_url']).name
                assert source_file.read_bytes() == base64.b64decode(original.split(',', 1)[1])
                assert source['time_sec'] == 0 and source['reference_frame_id'] == source_id
                assert source['reference_camera'] == camera
                assert '[[annotation:' + closed['id'] + ']]' in saved['note']
                assert store.scene() == scene and not errors, errors
                print('PASS: unfinished submission preserves the draft without posting; seeks cancel even on the same GT frame, round clear cancels its temporary path and undo restores marks/moments, right-pane tools and reload remain safe, and real HTTP feedback keeps original GT bytes/camera/time and schema-valid evidence', flush=True)
                browser.close()
        finally:
            server.shutdown(); thread.join(5); server.server_close()
    print('ALL LIVEWIRE BROWSER CHECKS PASSED', flush=True)


if __name__ == '__main__': main()
