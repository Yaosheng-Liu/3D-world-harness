#!/usr/bin/env python3
"""Check captured GLB colors with ImageBitmap unavailable, including worker previews."""
import argparse
import base64
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--web-dir', type=Path, default=ROOT / 'web')
parser.add_argument('--baseline', action='store_true', help='Demonstrate the unfixed server behavior without replacing its modules')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)


def picture(data):
    return Image.open(BytesIO(base64.b64decode(data.split(',', 1)[1]))).convert('RGB')


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--no-proxy-server',
        '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
    results, pictures = {}, {}
    for fault in (False, True):
        name = 'bitmap_failure' if fault else 'normal'
        context = browser.new_context(viewport={'width':1440, 'height':1000})
        if fault:
            context.add_init_script("window.createImageBitmap=()=>Promise.reject(new Error('Injected ImageBitmap decoder failure'))")

        def module(route):
            filename = route.request.url.rsplit('/', 1)[-1]
            response = route.fetch()
            body = response.text() if args.baseline else (args.web_dir / filename).read_text()
            if filename == 'app.js':
                body += '\nwindow.__textureCheck={state,renderer,objectLayer,camera,controls,selectionOutline,renderLiveScene,alignActiveReference};'
            route.fulfill(response=response, body=body)

        for filename in ('app.js', 'world-materials.js', 'scene-reference-preview-renderer.js'):
            context.route('**/' + filename, module)
        # This test exercises WebGL; it must never mistake a Cycles image for success.
        context.route('**/api/workspace/cycles-preview', lambda route: route.fulfill(
            json={'available':False, 'scene_revision':3}))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until='domcontentloaded')
        page.wait_for_function('window.__textureCheck?.state.workspaceReady && !__textureCheck.state.sceneLoading && __textureCheck.state.objectNodes.size>0', timeout=90000)
        page.locator('#compare-opacity').fill('0')
        page.locator('#compare-opacity').dispatch_event('input')
        page.evaluate('__textureCheck.alignActiveReference()')
        page.wait_for_timeout(500)
        result = page.evaluate('''() => {
          const m=__textureCheck, materials=new Set(), sources=new Set(), rows=[];
          m.objectLayer.traverse(n=>{for(const material of Array.isArray(n.material)?n.material:n.material?[n.material]:[])materials.add(material)});
          for(const material of materials){
            if(material.userData.world_appearance!=='captured')continue;
            if(material.map)sources.add(material.map.source);
            rows.push({name:material.name,textured:!!material.map?.image,width:material.map?.image?.width,height:material.map?.image?.height,
              flipY:material.map?.flipY,colorSpace:material.map?.colorSpace});
          }
          m.renderLiveScene();return {rows,unique_sources:sources.size,context_lost:m.renderer.getContext().isContextLost(),image:m.renderer.domElement.toDataURL('image/png')};
        }''')
        frame = picture(result.pop('image')); frame.save(args.output / (name + '.png'))
        rgb = np.asarray(frame, dtype=float)
        result['colored_fraction'] = float((rgb.max(axis=2)-rgb.min(axis=2)>30).mean())
        assert result['rows'] and not result['context_lost'] and not errors, (result, errors)
        expected_white = args.baseline and fault
        assert all(row['textured'] != expected_white for row in result['rows']), result
        if expected_white:
            assert result['colored_fraction'] < .02, result
        else:
            assert result['colored_fraction'] > .1, result
        if not args.baseline:
            assert all(max(row['width'],row['height'])<=4096 and row['flipY'] is False and row['colorSpace']=='srgb' for row in result['rows']), result
        if fault and not args.baseline:
            page.evaluate('''() => {let bed;__textureCheck.objectLayer.traverse(n=>{if(n.userData.stable_id==='main_bed')bed=n});
              if(!bed)throw Error('Missing main_bed');__textureCheck.selectionOutline.setSelection(bed);
              __textureCheck.camera.position.x+=.15;__textureCheck.controls.update();__textureCheck.renderLiveScene();}''')
            page.wait_for_timeout(500)
            page.screenshot(path=str(args.output/'selected.png'))
            selected = np.asarray(picture(page.evaluate('__textureCheck.renderer.domElement.toDataURL("image/png")')), dtype=float)
            result['selected_colored_fraction'] = float((selected.max(axis=2)-selected.min(axis=2)>30).mean())
            assert result['selected_colored_fraction'] > .1, result
            preview = page.evaluate('''async()=>{const {renderSceneReferencePreview}=await import('./scene-reference-preview.js');
              return renderSceneReferencePreview({scene:await fetch('/api/scene').then(r=>r.json())});}''')
            assert preview['width']==640 and preview['height']==400
            picture(preview['data_url']).save(args.output/'worker-preview.jpg')
            result['worker_preview'] = True
            result['corrupt_texture_error'] = page.evaluate('''async()=>{
              const {GLTFLoader}=await import('three/addons/loaders/GLTFLoader.js');
              const {configureWorldLoader}=await import('./world-materials.js');
              const buffer=await fetch(__textureCheck.state.sceneObjects[0].url).then(r=>r.arrayBuffer());
              const length=new DataView(buffer).getUint32(12,true);
              const gltf=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,20,length)));
              const image=gltf.bufferViews[gltf.images[0].bufferView];
              new Uint8Array(buffer,28+length+(image.byteOffset||0),image.byteLength).fill(0);
              const loader=new GLTFLoader();configureWorldLoader(loader);
              try {await loader.parseAsync(buffer,'');return null;}catch(error){return error.message;}
            }''')
            assert result['corrupt_texture_error'].startswith('实拍纹理加载失败'), result
        assert not errors, errors
        results[name], pictures[name] = result, rgb
        context.close()
    if not args.baseline:
        results['decoder_failure_pixel_mae'] = float(np.abs(pictures['normal']-pictures['bitmap_failure']).mean())
        assert results['decoder_failure_pixel_mae'] < 1, results
    (args.output/'result.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
    browser.close()
