#!/usr/bin/env python3
"""Place real annotation controls in image letterboxes without changing evidence."""
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
from workspace_ui_helpers import control


def photo(width,height):
    buffer=BytesIO();Image.new('RGB',(width,height),'#34496a').save(buffer,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()


def point(box,x=.5,y=.5):
    return {'x':box['x']+box['width']*x,'y':box['y']+box['height']*y}


def layout(page,pane):
    return page.evaluate('''pane=>{const stage=document.getElementById(pane+'-stage'),
      media=document.getElementById(pane==='reference'?'reference-media':__adaptive.state.sceneView==='snapshot'?'scene-snapshot-media':'viewport'),
      dock=document.getElementById('annotation-tool-panel');
      const box=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}};
      return {stage:box(stage),image:box(media),dock:box(dock),placement:dock.dataset.placement,
        context:dock.dataset.context,viewport:[innerWidth,innerHeight]};}''',pane)


def settle(page):
    page.evaluate('''async()=>{await Promise.all(document.getElementById('annotation-tool-panel').getAnimations().map(a=>a.finished.catch(()=>{})));
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}''')


def show(page,pane,*,right=False):
    from playwright.sync_api import expect
    dock=page.locator('#annotation-tool-panel')
    # A capture can still be animating/automatically closing its prior dock.
    # Enter the actual surface before clicking its close action so the hover
    # cancels its ordinary leave timer; use no forced click or state mutation.
    settle(page)
    if dock.is_visible():
        page.mouse.move(**point(dock.bounding_box()));settle(page)
        if dock.is_visible():page.locator('#annotation-tools-close').click()
    stage=page.locator('#'+pane+'-stage');stage.scroll_into_view_if_needed()
    page.mouse.move(0,0);current=layout(page,pane);s,image=current['stage'],current['image']
    top=max(10,s['y']+10)
    if pane=='scene' and page.locator('html').get_attribute('data-layout')=='immersive':
        header_bottom=page.evaluate('Math.max(document.querySelector(".scene-pane > .pane-head").getBoundingClientRect().bottom,document.querySelector(".topbar").getBoundingClientRect().bottom)')
        top=max(top,header_bottom+10)
    where={'x':(image['x']+image['width']+s['x']+s['width'])/2,'y':s['y']+s['height']/2} if right else {'x':s['x']+s['width']/2,'y':top+20}
    page.mouse.move(**where);expect(dock).to_be_visible()
    page.wait_for_function('pane=>document.getElementById("annotation-tool-panel").dataset.context===pane',arg=pane)
    settle(page)
    return layout(page,pane)


def assert_placement(current,expected=None,*,clear=True):
    dock,image,stage=current['dock'],current['image'],current['stage']
    if expected:assert current['placement']==expected,current
    assert current['placement'] in {'top','right'},current
    assert dock['x']>=0 and dock['y']>=0 and dock['x']+dock['width']<=current['viewport'][0]+1 and dock['y']+dock['height']<=current['viewport'][1]+1,current
    if clear:
        left=max(image['x'],stage['x']);right=min(image['x']+image['width'],stage['x']+stage['width'])
        top=max(image['y'],stage['y']);bottom=min(image['y']+image['height'],stage['y']+stage['height'])
        overlap_x=min(dock['x']+dock['width'],right)-max(dock['x'],left)
        overlap_y=min(dock['y']+dock['height'],bottom)-max(dock['y'],top)
        assert overlap_x<=1 or overlap_y<=1,current


def select_reference(page,reference):
    page.locator('#reference-strip .thumb[data-reference-id="'+reference['id']+'"]').click()
    page.wait_for_function('id=>__adaptive.state.activeReferenceId===id && __adaptive.controls.transition===null && document.getElementById("reference-image").complete',arg=reference['id'])
    settle(page)


def capture(page):
    control(page,'#capture-scene-button').click()
    page.wait_for_function('__adaptive.state.sceneView==="snapshot" && document.getElementById("scene-snapshot-image").complete')
    return page.evaluate('structuredClone(__adaptive.state.snapshot)')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable',default='/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    args=parser.parse_args()
    from playwright.sync_api import expect,sync_playwright
    with tempfile.TemporaryDirectory(prefix='adaptive-annotation-tools-') as temporary, \
         patch.object(WorkspaceGateway,'create_target',side_effect=AssertionError('layout created a task')) as tasks, \
         patch.object(WorkspaceGateway,'list_models',side_effect=AssertionError('layout queried a model')) as models:
        root=Path(temporary);project=root/'project';project.mkdir();asset=project/'model.glb';model(asset)
        server=make_server(port=0,data_dir=root/'data',project_dir=project,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        store=server.scene_store;session=server.workspace_gateway.ensure()['session_id'];store.import_model(str(asset),object_id='model',name='Model')
        refs=[]
        for name,width,height in [('portrait',240,960),('landscape',960,240),('filled',700,800)]:
            ref=store.add_reference(session,name+'.png',photo(width,height));refs.append(ref)
            store.set_reference_cameras(session,[{'reference_id':ref['id'],'camera':{
                'camera_to_world':[[1,0,0,0],[0,1,0,0],[0,0,1,3],[0,0,0,1]],
                'intrinsics':{'width':width,'height':height,'fx':400,'fy':400,'cx':width/2,'cy':height/2},'image_undistorted':True}}])
        original_scene=copy.deepcopy(store.scene());errors=[];writes=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,executable_path=args.browser_executable,args=[
                    '--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                context=browser.new_context(viewport={'width':1440,'height':1000})
                hook='\nwindow.__adaptive={state,cameraData,controls,saveMomentDraft};'
                context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda call:writes.append(call.url) if call.method not in {'GET','HEAD','OPTIONS'} else None)
                page.goto(server.browser_url(session))
                page.wait_for_function('window.__adaptive?.state.workspaceReady && !__adaptive.state.sceneLoading && __adaptive.controls.transition===null && document.getElementById("reference-image").naturalWidth>0')
                if page.locator('#chat-dock').is_visible():control(page,'#chat-collapse').click()
                portrait=show(page,'reference',right=True);assert_placement(portrait,'right')
                point_button=page.locator('#annotation-tool-panel [data-tool="point"]').bounding_box()
                rectangle_button=page.locator('#annotation-tool-panel [data-tool="rectangle"]').bounding_box()
                assert rectangle_button['y']>point_button['y']+20 and abs(rectangle_button['x']-point_button['x'])<8
                assert_placement(show(page,'scene'),'right')
                tall=capture(page);assert_placement(show(page,'scene',right=True),'right')
                page.screenshot(path='/tmp/adaptive_tools_062.png')
                print('PASS default-motion portrait reference, calibrated live view and fixed screenshot use a vertical right dock; actual right blank hover and legacy top entry both open tools clear of the image',flush=True)

                select_reference(page,refs[1]);wide=show(page,'reference');assert_placement(wide,'top')
                point_button=page.locator('#annotation-tool-panel [data-tool="point"]').bounding_box()
                rectangle_button=page.locator('#annotation-tool-panel [data-tool="rectangle"]').bounding_box()
                assert rectangle_button['x']>point_button['x']+20 and abs(rectangle_button['y']-point_button['y'])<8
                capture(page);assert_placement(show(page,'scene'),'top')
                page.locator('.snapshot-open[data-snapshot-id="'+tall['id']+'"]').click()
                assert_placement(show(page,'scene',right=True),'right')
                assert_placement(show(page,'reference'),'top')
                print('PASS landscape top dock has horizontal buttons; left landscape and retained right portrait independently choose clear top/right letterboxes',flush=True)

                select_reference(page,refs[2]);assert_placement(show(page,'reference'),'top',clear=False)
                frozen=capture(page);assert_placement(show(page,'scene'),'top',clear=False)
                image=layout(page,'scene')['image'];page.mouse.move(**point(image))
                for _ in range(3):page.mouse.wheel(0,300)
                page.wait_for_function('__adaptive.state.snapshotViewports.get(__adaptive.state.snapshot.id)?.zoom<.7')
                assert_placement(show(page,'scene'))
                page.locator('#annotation-tool-panel [data-tool="line"]').click()
                page.locator('#scene-annotations').focus();page.keyboard.down('Space')
                before=layout(page,'scene');start=point(before['image']);page.mouse.move(**start);page.mouse.down()
                locked=layout(page,'scene');dx=before['stage']['x']+60-before['image']['x'];dy=before['stage']['y']+8-before['image']['y']
                page.mouse.move(start['x']+dx,start['y']+dy,steps=8)
                during=layout(page,'scene');assert during['placement']==locked['placement'] and during['dock']==locked['dock']
                page.mouse.up();page.keyboard.up('Space')
                right=show(page,'scene',right=True);assert_placement(right,'right')
                summary=page.locator('#more-tools > summary');summary.click();locked=layout(page,'scene')
                page.set_viewport_size({'width':1420,'height':1000});settle(page)
                assert layout(page,'scene')['placement']==locked['placement'] and layout(page,'scene')['dock']==locked['dock']
                page.locator('#more-tools [data-tool="freehand"]').click()
                assert page.evaluate('__adaptive.state.paneModes.scene')=='freehand';summary.click()
                page.locator('#annotation-tool-panel [data-tool="line"]').click()
                image=layout(page,'scene')['image'];start=point(image,.33,.44);end=point(image,.58,.6)
                page.mouse.move(**start);page.mouse.down();locked=layout(page,'scene')
                page.set_viewport_size({'width':1440,'height':1000});settle(page)
                assert layout(page,'scene')['placement']==locked['placement'] and layout(page,'scene')['dock']==locked['dock']
                end=point(layout(page,'scene')['image'],.58,.6);page.mouse.move(**end,steps=8);page.mouse.up()
                page.wait_for_function('__adaptive.state.annotations.length===1');mark=page.evaluate('structuredClone(__adaptive.state.annotations[0])')
                assert max(abs(mark['coordinates'][key]-value) for key,value in zip(('x','y','x2','y2'),(.33,.44,.58,.6)))<.006
                assert mark['snapshot_id']==frozen['id'] and page.evaluate('structuredClone(__adaptive.state.snapshot)')==frozen
                control(page,'#drag-scene-image').click();reference=page.evaluate('structuredClone(__adaptive.state.imageRefs[0])')
                assert reference['original_data_url']==frozen['data_url'] and reference['camera']==frozen['camera']
                print('PASS no-blank fallback, wheel shrink and pan reposition; active pan/drawing/menu keep controls stable and clickable through resize, source annotation coordinates and quoted original/camera stay exact',flush=True)

                if page.locator('#chat-dock').is_visible():control(page,'#chat-collapse').click()
                page.set_viewport_size({'width':390,'height':844});assert_placement(show(page,'reference'),clear=False)
                assert_placement(show(page,'scene'),clear=False)
                page.set_viewport_size({'width':1440,'height':1000})
                control(page,'#immersive-toggle').click();assert_placement(show(page,'scene'),clear=False)
                assert page.locator('html').get_attribute('data-layout')=='immersive'
                expect(page.locator('#annotation-tool-panel [data-tool="point"]')).to_be_visible()
                assert store.scene()==original_scene and not errors and not writes,(errors,writes)
                tasks.assert_not_called();models.assert_not_called()
                print('PASS phone and immersive layout keep the actual tool surface inside the visible viewport with usable top/right fallback; source scene unchanged, no browser errors, writes, feedback or model calls',flush=True)
                browser.close()
        finally:server.shutdown();thread.join(5);server.server_close()
    print('ALL ADAPTIVE ANNOTATION TOOL CHECKS PASSED',flush=True)


if __name__=='__main__':main()
