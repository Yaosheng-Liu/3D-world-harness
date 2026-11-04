#!/usr/bin/env python3
"""Prepare a synthetic Blender fixture, or test its running editing UI with Chromium."""
import argparse
from copy import deepcopy
import json
import io
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def prepare(project, blender):
    from core import SceneStore
    from world_build import build_world
    from world_model import WorldStore, scene_hash
    from world_service import WorldService
    scene = json.loads((ROOT / "examples/physical_world/scene.json").read_text())
    WorldStore(project).initialize(scene)
    candidate = deepcopy(scene)
    candidate["revision"] = 2
    candidate["objects"][0]["material"]["base_color"] = [.2, .25, .3, 1]
    receipt = build_world(project, scene=candidate, render=False, blender=blender)
    store = SceneStore(project / "data")
    WorldService(project, store).preview(str(Path(receipt["build_dir"]) / "receipt.json"), store.scene()["revision"], "Synthetic edit fixture")
    (project / "fixture.json").write_text(json.dumps({"world_hash":scene_hash(scene), "displayed":store.scene(), "build":receipt}, indent=2))
    print(json.dumps({"project":str(project), "objects":len(candidate["objects"])}))


def browser_check(url, output):
    from playwright.sync_api import sync_playwright
    from PIL import Image, ImageChops, ImageStat
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--no-proxy-server', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={"width":1500, "height":1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.locator('#flow-editor-open').wait_for(state='visible', timeout=60000)
        # Avoid background renders competing with the editing workspace.
        if page.locator('#cycles-auto').is_checked(): page.locator('#cycles-auto').uncheck()
        page.locator('#flow-editor-open').click()
        page.locator('#edit-create').wait_for(state='visible')
        page.wait_for_function("!document.querySelector('#edit-create').disabled")
        page.locator('#edit-name').fill('浏览器编辑验证')
        page.locator('#edit-create').click()
        page.wait_for_function("document.querySelector('#edit-status').textContent.startsWith('已保存')", timeout=90000)
        identifier = page.locator('#edit-branch').input_value()
        api = url.rstrip('/') + '/api/workspace/scene-editor/' + identifier
        initial = page.request.get(api).json()
        page.locator('#edit-objects').select_option('block')
        page.locator('#edit-focus').click()
        before = page.locator('#edit-canvas canvas').screenshot()
        for i, value in enumerate(['-0.3', '-0.25', '1.0']): page.locator('#edit-position input').nth(i).fill(value)
        for i, value in enumerate(['10', '20', '30']): page.locator('#edit-rotation input').nth(i).fill(value)
        page.locator('#edit-scale').fill('1.5')
        page.locator('#edit-color').fill('#3366cc')
        page.locator('#edit-roughness').fill('0.25')
        assert page.locator('#edit-save').is_enabled()
        assert page.locator('#edit-render').is_disabled()
        assert page.locator('#edit-canvas canvas').screenshot() != before
        page.locator('#edit-focus').click()
        live = Image.open(io.BytesIO(page.locator('#edit-canvas canvas').screenshot())).convert('RGB')
        page.screenshot(path=str(output / 'editing-live.png'))
        # Uncommitted changes survive attempted object switching.
        page.locator('#edit-objects').select_option('table')
        assert page.locator('#edit-objects').input_value() == 'block'
        page.locator('#edit-save').click()
        page.wait_for_function("document.querySelector('#edit-status').textContent.startsWith('已保存') && !document.querySelector('#edit-undo').disabled", timeout=180000)
        changed = page.request.get(api).json()
        old = {obj['id']:obj for obj in initial['scene']['objects']}
        new = {obj['id']:obj for obj in changed['scene']['objects']}
        assert new['block']['position'] == [-.3, -.25, 1.0]
        assert new['block']['material']['roughness'] == .25
        assert new['block']['visual'][0]['size'] == [v*1.5 for v in old['block']['visual'][0]['size']]
        assert all(old[key] == new[key] for key in old if key != 'block')
        saved = Image.open(io.BytesIO(page.locator('#edit-canvas canvas').screenshot())).convert('RGB')
        preview_error = max(ImageStat.Stat(ImageChops.difference(live, saved)).mean)
        assert preview_error < 2, f'Live preview differs from saved Blender geometry: {preview_error}'
        page.locator('#edit-undo').click()
        page.wait_for_function("!document.querySelector('#edit-redo').disabled", timeout=60000)
        assert page.request.get(api).json()['scene'] == initial['scene']
        page.locator('#edit-redo').click()
        page.wait_for_function("!document.querySelector('#edit-undo').disabled && document.querySelector('#edit-redo').disabled", timeout=60000)
        assert page.request.get(api).json()['scene'] == changed['scene']
        page.locator('#edit-camera').click()
        page.locator('#edit-render').click()
        page.wait_for_function("document.querySelector('#edit-rendered').hidden === false", timeout=120000)
        page.screenshot(path=str(output / 'editing-cycles.png'))
        page.locator('#edit-live').click()
        # Pick a visible object with the raycaster instead of the object list.
        page.locator('#edit-objects').select_option('block'); page.locator('#edit-focus').click()
        page.locator('#edit-objects').select_option('table')
        box = page.locator('#edit-canvas').bounding_box()
        page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
        assert page.locator('#edit-objects').input_value() == 'block'
        page.locator('#edit-close').click()
        page.reload(); page.locator('#flow-editor-open').wait_for(state='visible', timeout=60000)
        page.locator('#flow-editor-open').click()
        page.wait_for_function("document.querySelector('#edit-status').textContent.startsWith('已保存')", timeout=60000)
        assert page.locator('#edit-branch').input_value() == identifier
        page.screenshot(path=str(output / 'editing-desktop.png'))
        page.set_viewport_size({"width":390, "height":844})
        assert page.locator('#scene-editor').evaluate('e => e.scrollWidth <= e.clientWidth + 2')
        page.screenshot(path=str(output / 'editing-mobile.png'))
        assert not errors, errors
        result = {"branch_id":identifier, "version":page.request.get(api).json()['version'],
                  "live_preview":True, "object_picking":True, "save_undo_redo":True,
                  "cycles":True, "reload_persistence":True, "preview_mean_pixel_error":preview_error, "page_errors":errors}
        (output / 'result.json').write_text(json.dumps(result, indent=2)); print(json.dumps(result, indent=2))
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-project', type=Path)
    parser.add_argument('--blender', default='blender')
    parser.add_argument('--url')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.prepare_project: prepare(args.prepare_project, args.blender)
    elif args.url and args.output: browser_check(args.url, args.output)
    else: parser.error('use --prepare-project, or --url and --output')
