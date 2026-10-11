#!/usr/bin/env python3
"""Zoom real fixed screenshots without moving their camera or evidence pixels."""
from __future__ import annotations

import argparse
import base64
import copy
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests')]
from gateway import WorkspaceGateway
from server import make_server
from test_object_double_click_browser import model
from test_prompt_drag_browser import image_data
from test_selection_outline_browser import model as animated_model
from workspace_ui_helpers import choose_tool,control


def ready(page):
    page.wait_for_function('window.__snapshotZoom?.state.workspaceReady && !__snapshotZoom.state.sceneLoading && '
        '!__snapshotZoom.state.seeking && !__snapshotZoom.state.submitting && __snapshotZoom.controls.transition===null && '
        'document.getElementById("reference-image").naturalWidth>0')


def picture(data_url):
    return Image.open(BytesIO(base64.b64decode(data_url.split(',',1)[1]))).convert('RGB')


def near_red(image,point):
    x,y=round(point[0]*image.width),round(point[1]*image.height)
    region=image.crop((max(0,x-8),max(0,y-8),min(image.width,x+9),min(image.height,y+9)))
    return sum(r>110 and r>g+25 and r>b+25 for r,g,b in getattr(region,'get_flattened_data',region.getdata)())


def geometry(page):
    return page.evaluate('''()=>{const image=document.getElementById('scene-snapshot-image'),canvas=document.getElementById('scene-annotations'),
      overlay=document.getElementById('snapshot-compare-image'),stage=document.getElementById('scene-stage');
      const box=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};
      return {image:box(image),canvas:box(canvas),overlay:box(overlay),stage:box(stage),
        logical:[canvas.clientWidth,canvas.clientHeight],bitmap:[canvas.width,canvas.height],
        scroll:[stage.scrollLeft,stage.scrollTop],
        transform:document.getElementById('scene-snapshot-media').style.transform};}''')


def normalize(point,rect):
    return [(point['x']-rect['x'])/rect['width'],(point['y']-rect['y'])/rect['height']]


def point(rect,x,y):
    return {'x':rect['x']+rect['width']*x,'y':rect['y']+rect['height']*y}


def close_chat(page):
    if page.locator('#chat-dock').is_visible():control(page,'#chat-collapse').click()


def capture(page):
    control(page,'#capture-scene-button').click()
    page.wait_for_function('__snapshotZoom.state.sceneView==="snapshot" && document.getElementById("scene-snapshot-image").complete && document.getElementById("scene-snapshot-image").naturalWidth>0')
    return page.evaluate('structuredClone(__snapshotZoom.state.snapshot)')


def media_bytes(store,url):
    return (store.media_dir/Path(url).name).read_bytes()


def wheel_snapshot(page,delta):
    count=page.evaluate('__snapshotZoom.wheelCount')
    page.mouse.wheel(0,delta)
    page.wait_for_function('count=>__snapshotZoom.wheelCount>count',arg=count)
    page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')


def pan_snapshot(page):
    page.locator('#scene-annotations').focus();page.keyboard.down('Space')
    previous=geometry(page);pan_start=point(previous['stage'],.57,.44)
    page.mouse.move(**pan_start);page.mouse.down()
    page.mouse.move(pan_start['x']+42,pan_start['y']-31,steps=8);page.mouse.up();page.keyboard.up('Space')
    panned=geometry(page)
    assert abs(panned['image']['x']-previous['image']['x']-42)<2 and abs(panned['image']['y']-previous['image']['y']+31)<2,{
        'before':previous,'after':panned,'start':pan_start,
        'pan':page.evaluate('[...__snapshotZoom.state.snapshotViewports]'),'marks':page.evaluate('__snapshotZoom.state.annotations')}
    return panned


def run_case(pw,root,args,*,dynamic=False):
    from playwright.sync_api import expect
    label='dynamic' if dynamic else 'static';project=root/label;project.mkdir()
    asset=project/'model.glb';(animated_model if dynamic else model)(asset)
    server=make_server(port=0,data_dir=root/(label+'-data'),project_dir=project,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    store=server.scene_store;gateway=server.workspace_gateway;session=gateway.ensure()['session_id']
    store.import_model(str(asset),object_id='model',name='Preview model')
    camera={'camera_to_world':[[1,0,0,0],[0,1,0,0],[0,0,1,3],[0,0,0,1]],
        'intrinsics':{'width':320,'height':240,'fx':300,'fy':300,'cx':160,'cy':120}}
    if dynamic:
        store.set_reference_clip(session,{'name':'GT sequence','fps':2,'frames':[
            {'name':'frame0.png','data_url':image_data('navy'),'time_sec':0,'camera':camera},
            {'name':'frame1.png','data_url':image_data('blue'),'time_sec':.5,'camera':camera}]})
    else:
        gt=project/'gt.png';gt.write_bytes(base64.b64decode(image_data('navy').split(',',1)[1]))
        gateway.add_reference_paths([str(gt)])
    original_scene=copy.deepcopy(store.scene());errors=[];posted=[]
    try:
        browser=pw.chromium.launch(headless=True,executable_path=args.browser_executable,args=[
            '--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=2)
        hook='\nwindow.__snapshotZoom={state,cameraData,controls,saveMomentDraft,captureScene,wheelCount:0};ui.sceneStage.addEventListener("wheel",()=>__snapshotZoom.wheelCount++,{capture:true});'
        context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda call:posted.append(call.post_data_json) if call.method=='POST' and call.url.endswith('/feedback') else None)
        page.goto(server.browser_url(session));ready(page);close_chat(page)
        first=capture(page);before=geometry(page);original=first['data_url']
        camera_before=page.evaluate('__snapshotZoom.cameraData()')
        anchor=point(before['image'],.47,.34)
        page.mouse.move(**anchor)
        for _ in range(3):
            width=geometry(page)['image']['width'];page.mouse.wheel(0,-400)
            page.wait_for_function('width=>document.getElementById("scene-snapshot-image").getBoundingClientRect().width>width*1.05',arg=width)
        after=geometry(page)
        assert after['image']['width']>before['image']['width']*1.3
        assert max(abs(a-b) for a,b in zip(normalize(anchor,after['image']),normalize(anchor,before['image'])))<.004
        assert after['bitmap']==before['bitmap'],'display zoom must not allocate a zoom-sized annotation bitmap'
        assert after['image']==after['canvas']
        assert page.evaluate('__snapshotZoom.cameraData()')==camera_before
        assert page.evaluate('structuredClone(__snapshotZoom.state.snapshot)')==first
        for rect in (before,after):
            image,overlay=rect['image'],rect['overlay']
            observed=[(overlay['x']-image['x'])/image['width'],(overlay['y']-image['y'])/image['height'],
                overlay['width']/image['width'],overlay['height']/image['height']]
            expected=[first['comparison']['rect'][key] for key in ('x','y','width','height')]
            assert max(abs(a-b) for a,b in zip(observed,expected))<.004

        # Panning while holding Space takes precedence over the active drawing
        # tool and never creates a mark or changes the frozen evidence camera.
        tool='arrow' if dynamic else 'line';choose_tool(page,tool,'scene')
        panned=pan_snapshot(page)
        assert page.evaluate('__snapshotZoom.state.annotations.length')==0
        assert page.evaluate('__snapshotZoom.cameraData()')==camera_before

        # Cross 1x with a nonzero pan: resetting its translation would make the
        # same source pixel jump away from this fixed, real wheel position.
        pivot={key:round(value) for key,value in point(panned['image'],.61,.57).items()}
        page.mouse.move(**pivot)
        for _ in range(30):
            prior=geometry(page);ratio=prior['image']['width']/prior['logical'][0]
            if ratio<.101:break
            source_point=normalize(pivot,prior['image']);wheel_snapshot(page,300)
            smaller=geometry(page)
            assert smaller['image']['width']<prior['image']['width']
            assert max(abs(a-b) for a,b in zip(source_point,normalize(pivot,smaller['image'])))<.004
        minimum=geometry(page)
        assert abs(minimum['image']['width']/minimum['logical'][0]-.1)<.002
        for _ in range(2):wheel_snapshot(page,300)
        assert geometry(page)==minimum,'additional wheel input at 0.1x must not shift the viewport'
        for _ in range(20):
            if geometry(page)['image']['width']/geometry(page)['logical'][0]>=.6:break
            wheel_snapshot(page,-300)
        smaller=geometry(page)
        assert .6<=smaller['image']['width']/smaller['logical'][0]<.7
        assert smaller['bitmap']==before['bitmap']
        panned=pan_snapshot(page)
        assert page.evaluate('__snapshotZoom.state.annotations.length')==0
        assert page.evaluate('__snapshotZoom.cameraData()')==camera_before
        start=point(panned['image'],.42,.34);end=point(panned['image'],.55,.46)
        page.mouse.move(**start);page.mouse.down();page.mouse.move(**end,steps=10);page.mouse.up()
        page.wait_for_function('__snapshotZoom.state.annotations.length===1')
        mark=page.evaluate('structuredClone(__snapshotZoom.state.annotations[0])')
        assert mark['type']==tool and mark['snapshot_id']==first['id']
        assert max(abs(mark['coordinates'][key]-expected) for key,expected in zip(('x','y','x2','y2'),(.42,.34,.55,.46)))<.004
        overlay=picture(page.locator('#scene-annotations').evaluate('el=>el.toDataURL("image/png")'))
        assert near_red(overlay,(.485,.40))>8
        assert page.evaluate('structuredClone(__snapshotZoom.state.snapshot)')==first
        viewport=panned['transform']
        print(f'PASS {label}: real wheel crosses 1x without a pixel jump, reaches a stable 0.1x limit, then 0.6x Space pan and {tool} use source coordinates; camera/evidence/annotation resolution stay fixed at DPR 2',flush=True)

        page.locator('#scene-live-card').click();page.wait_for_function('__snapshotZoom.state.sceneView==="live" && __snapshotZoom.controls.enabled')
        current=page.evaluate('__snapshotZoom.cameraData().position')
        live=page.locator('#viewport canvas').bounding_box();page.mouse.move(**point(live,.6,.36));page.mouse.wheel(0,-180)
        page.wait_for_function('before=>__snapshotZoom.cameraData().position.some((value,index)=>Math.abs(value-before[index])>1e-6)',arg=current)
        if dynamic:
            page.locator('#timeline-next').click();page.wait_for_function('__snapshotZoom.state.time===.5 && !__snapshotZoom.state.seeking')
        second=capture(page);assert second['id']!=first['id']
        second_geometry=geometry(page)
        assert abs(second_geometry['image']['width']/second_geometry['logical'][0]-1)<.004
        page.mouse.move(**point(second_geometry['image'],.5,.38));page.mouse.wheel(0,-300)
        page.wait_for_function('width=>document.getElementById("scene-snapshot-image").getBoundingClientRect().width>width*1.05',arg=second_geometry['image']['width'])
        page.locator('.snapshot-open[data-snapshot-id="'+first['id']+'"]').click()
        page.wait_for_function('id=>__snapshotZoom.state.snapshot?.id===id && !__snapshotZoom.state.seeking',arg=first['id'])
        assert geometry(page)['transform']==viewport
        page.evaluate('__snapshotZoom.saveMomentDraft.pending?.then(()=>true)')
        page.reload();ready(page)
        page.wait_for_function('id=>__snapshotZoom.state.sceneView==="snapshot" && __snapshotZoom.state.snapshot?.id===id && document.getElementById("scene-snapshot-image").complete',arg=first['id'])
        assert geometry(page)['transform']==viewport
        assert page.evaluate('structuredClone(__snapshotZoom.state.snapshot)')==first
        assert page.evaluate('structuredClone(__snapshotZoom.state.annotations[0])')==mark
        print(f'PASS {label}: live wheel still moves the 3D camera, separate snapshots keep independent zoom/pan, reopening and draft reload preserve the first viewport and its annotations',flush=True)

        # Cite the enlarged screenshot using the actual composer menu. Its
        # original bytes and sent camera must still belong to the complete image.
        control(page,'#drag-scene-image').click()
        reference=page.evaluate('structuredClone(__snapshotZoom.state.imageRefs[0])')
        assert reference['original_data_url']==original and reference['camera']==first['camera']
        bundle=page.evaluate('async()=>__snapshotZoom.captureScene(__snapshotZoom.state.snapshot)')
        assert bundle['scene_original_data_url']==original
        assert near_red(picture(bundle['scene_annotated_data_url']),(.485,.40))>8
        with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/feedback')) as response:
            page.locator('#submit-button').click()
        assert response.value.status==201,response.value.text()
        saved=response.value.json();frames=saved['dynamic_frames' if dynamic else 'scene_snapshots']
        frozen=next(frame for frame in frames if frame['id']==first['id'])
        assert media_bytes(store,frozen['scene_original_url'])==base64.b64decode(original.split(',',1)[1])
        assert frozen['camera']==first['camera']
        assert near_red(Image.open(BytesIO(media_bytes(store,frozen['scene_annotated_url']))).convert('RGB'),(.485,.40))>8
        assert len(saved['image_refs'])==1 and media_bytes(store,saved['image_refs'][0]['original_url'])==base64.b64decode(original.split(',',1)[1])
        assert saved['annotations'][0]['coordinates']==mark['coordinates']
        if dynamic:
            assert frozen['time_sec']==first['time_sec'] and frozen['reference_frame_id']==first['reference_frame_id']
            assert frozen['reference_original_url']==first['reference_url']
        assert len(posted)==1
        assert all('snapshotViewports' not in frame and 'zoom' not in frame and 'pan' not in frame for frame in frames)
        assert store.scene()==original_scene and not errors,errors
        print(f'PASS {label}: quoted and HTTP-submitted original pixels/camera/GT remain exact; annotated evidence keeps source coordinates and display transforms stay out of feedback',flush=True)
        browser.close()
    finally:server.shutdown();thread.join(5);server.server_close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable',default='/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='snapshot-zoom-browser-') as temporary, \
         patch.object(WorkspaceGateway,'create_target',side_effect=AssertionError('zoom created a task')) as tasks, \
         patch.object(WorkspaceGateway,'list_models',side_effect=AssertionError('zoom queried models')) as models:
        with sync_playwright() as pw:
            for dynamic in (False,True):run_case(pw,Path(temporary),args,dynamic=dynamic)
        tasks.assert_not_called();models.assert_not_called()
    print('ALL SNAPSHOT ZOOM BROWSER CHECKS PASSED',flush=True)


if __name__=='__main__':main()
