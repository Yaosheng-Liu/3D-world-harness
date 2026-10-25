#!/usr/bin/env python3
"""Check a textured reconstruction with imported lights in the live workbench."""
import argparse
import base64
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--no-proxy-server',
        '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
    page = browser.new_page(viewport={'width':1440, 'height':1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def module(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text() + '\nwindow.__lightingCheck={state,renderer,objectLayer,camera,controls,renderLiveScene,alignActiveReference};')

    page.route('**/app.js', module)
    page.route('**/api/workspace/cycles-preview', lambda route: route.fulfill(json={'available':False}))
    page.goto(args.url, wait_until='domcontentloaded')
    page.wait_for_function('window.__lightingCheck?.state.workspaceReady && !__lightingCheck.state.sceneLoading && __lightingCheck.state.objectNodes.size>0', timeout=90000)
    page.locator('#compare-opacity').fill('0')
    page.locator('#compare-opacity').dispatch_event('input')
    page.evaluate('__lightingCheck.alignActiveReference()')
    results = {}
    for view in ('reference', 'navigated'):
        if view == 'navigated':
            page.evaluate('''() => {const m=__lightingCheck;
              m.camera.position.x+=.3;m.controls.update();}''')
        result = page.evaluate('''() => {
          const m=__lightingCheck,lights=[],materials=new Set();
          m.objectLayer.traverse(n=>{if(n.isLight)lights.push({name:n.name,visible:n.visible,intensity:n.intensity});
            for(const material of Array.isArray(n.material)?n.material:n.material?[n.material]:[])materials.add(material)});
          const surfaces=[...materials].filter(mat=>mat.userData.world_appearance==='surface');
          m.renderLiveScene();return {lights,surface_count:surfaces.length,
            textures_intact:surfaces.every(mat=>mat.map?.image && mat.isMeshStandardMaterial && mat.toneMapped),
            context_lost:m.renderer.getContext().isContextLost(),image:m.renderer.domElement.toDataURL()};
        }''')
        picture = Image.open(BytesIO(base64.b64decode(result.pop('image').split(',', 1)[1]))).convert('RGB')
        picture.save(args.output / (view + '.png'))
        pixels = np.asarray(picture, dtype=float)
        result['white_fraction'] = float((pixels.min(axis=2)>245).mean())
        result['colored_fraction'] = float((pixels.max(axis=2)-pixels.min(axis=2)>30).mean())
        assert result['lights'] and all(not light['visible'] for light in result['lights']), result
        assert result['surface_count'] and result['textures_intact'] and not result['context_lost'], result
        assert result['white_fraction'] < .25 and result['colored_fraction'] > .1, result
        results[view] = result
    assert not errors, errors
    (args.output/'result.json').write_text(json.dumps(results, indent=2)+'\n')
    print(json.dumps(results, indent=2))
    browser.close()
