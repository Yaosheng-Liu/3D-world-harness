#!/usr/bin/env python3
"""Preview actual drawn marks without changing saved evidence or opening a scene."""
from pathlib import Path
import sys,tempfile,threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests')]
from server import make_server
from test_object_double_click_browser import model
from test_prompt_drag_browser import image_data
from test_stable_gallery_browser import HOOK,ready,evidence,geometry,stable
from workspace_ui_helpers import control,choose_tool
from playwright.sync_api import sync_playwright,expect


def point(page,pane,x,y):
    choose_tool(page,'point',pane)
    canvas=page.locator('#'+pane+'-annotations')
    rect=canvas.bounding_box()
    page.mouse.click(rect['x']+rect['width']*x,rect['y']+rect['height']*y)
    return page.evaluate('structuredClone(__galleryCheck.state.annotations.at(-1))')


def ink(canvas,mark):
    return canvas.evaluate('''(canvas,mark)=>{
      const x=Math.round(mark.coordinates.x*canvas.width),y=Math.round(mark.coordinates.y*canvas.height);
      const pixels=canvas.getContext('2d').getImageData(x-3,y-3,7,7).data;
      let red=0;
      for(let i=0;i<pixels.length;i+=4) if(pixels[i]>110 && pixels[i]>pixels[i+1]+25 && pixels[i]>pixels[i+2]+25 && pixels[i+3]>100) red++;
      return red;
    }''',mark)


def show(page,index=0):
    page.mouse.move(2,2)
    card=page.locator('.snapshot-open[data-snapshot-id]').nth(index)
    card.hover()
    preview=page.locator('#snapshot-card-preview')
    expect(preview).to_be_visible()
    expect(preview.locator('canvas')).to_have_attribute('data-ready','true')
    page.wait_for_timeout(180)
    return card,preview


def main():
    with tempfile.TemporaryDirectory(prefix='gallery-marks-') as directory:
        root=Path(directory);project=root/'project';project.mkdir()
        server=make_server(port=0,project_dir=project,data_dir=root/'data',web_dir=ROOT/'web',enable_codex=False)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        store=server.scene_store;session=server.workspace_gateway.ensure()['session_id']
        asset=root/'scene.glb';model(asset);store.import_model(str(asset),object_id='test_model',name='柜子')
        errors=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=2)
                context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='text/javascript',body=(ROOT/'web/app.js').read_text()+HOOK))
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(server.browser_url(session));ready(page)
                control(page,'#chat-collapse').click();page.wait_for_timeout(450)
                marks=[]
                for x,y in [(.23,.35),(.73,.72)]:
                    control(page,'#capture-scene-button').click();ready(page)
                    marks.append(point(page,'scene',x,y))
                    control(page,'#scene-live-card').click()
                page.mouse.move(2,2);ready(page);saved=evidence(page);before=geometry(page)
                card,preview=show(page)
                canvas=preview.locator('canvas')
                assert ink(canvas,marks[0])>0 and ink(canvas,marks[1])==0
                expect(preview.locator('.snapshot-preview-count')).to_have_text('1')
                assert preview.locator('img').get_attribute('src')==page.evaluate('__galleryCheck.state.sceneSnapshots[0].data_url')
                # Physically cross the gap, then operate both controls without closing.
                target=preview.locator('[data-preview-mode=original]').bounding_box()
                page.mouse.move(target['x']+target['width']/2,target['y']+target['height']/2,steps=8)
                page.wait_for_timeout(550);expect(preview).to_be_visible()
                preview.locator('[data-preview-mode=original]').click();expect(canvas).to_be_hidden()
                preview.locator('[data-preview-mode=marked]').click();expect(canvas).to_be_visible()
                assert evidence(page)==saved;stable(before,geometry(page))
                page.screenshot(path='/tmp/gallery-marked-preview.png')
                page.mouse.move(2,2);expect(preview).to_be_hidden()
                _,preview=show(page,1);canvas=preview.locator('canvas')
                assert ink(canvas,marks[1])>0 and ink(canvas,marks[0])==0
                assert evidence(page)==saved
                # Keyboard entry and return must not select/open the screenshot.
                page.keyboard.press('Escape');page.locator('#scene-live-card').focus()
                page.keyboard.press('ArrowRight');expect(preview).to_be_visible()
                page.keyboard.press('Tab');expect(preview.locator('[data-preview-mode=marked]')).to_be_focused()
                page.keyboard.press('Tab');page.keyboard.press('Enter');expect(preview.locator('canvas')).to_be_hidden()
                page.keyboard.press('Escape');expect(card).to_be_focused();expect(preview).to_be_hidden()
                assert evidence(page)==saved
                # Separate video reference/scene marks, retaining the saved shared time.
                store.set_reference_clip(session,{'name':'参考视频','fps':2,'frames':[
                    {'name':f'frame{i}.png','data_url':image_data('navy'),'time_sec':i*.5} for i in range(3)]})
                page.reload();ready(page)
                control(page,'#scene-live-card').click();control(page,'#capture-scene-button').click();ready(page)
                scene_mark=point(page,'scene',.28,.32)
                ref_mark=point(page,'reference',.75,.75)
                assert scene_mark['frame_id']==ref_mark['frame_id']
                control(page,'#scene-live-card').click();page.mouse.move(2,2);ready(page)
                saved=evidence(page);before=geometry(page)
                _,preview=show(page,2);canvas=preview.locator('canvas')
                assert ink(canvas,scene_mark)>0 and ink(canvas,ref_mark)==0,{
                    'scene':scene_mark,'reference':ref_mark,'ink':[ink(canvas,scene_mark),ink(canvas,ref_mark)],
                    'preview':preview.get_attribute('aria-label'),
                    'comparison':page.evaluate('__galleryCheck.state.dynamicSnapshots[0].comparison && {enabled:__galleryCheck.state.dynamicSnapshots[0].comparison.enabled,opacity:__galleryCheck.state.dynamicSnapshots[0].comparison.opacity}')}
                preview.locator('[data-preview-layer="1"]').click()
                expect(canvas).to_have_attribute('data-ready','true')
                assert ink(canvas,ref_mark)>0 and ink(canvas,scene_mark)==0
                expect(preview.locator('.snapshot-preview-count')).to_have_text('1')
                page.screenshot(path='/tmp/gallery-reference-marks.png')
                preview.locator('[data-preview-mode=original]').click();expect(canvas).to_be_hidden()
                preview.locator('[data-preview-layer="0"]').click();expect(canvas).to_be_hidden()
                preview.locator('[data-preview-mode=marked]').click();expect(canvas).to_be_visible()
                expect(canvas).to_have_attribute('data-ready','true');assert ink(canvas,scene_mark)>0
                assert evidence(page)==saved;stable(before,geometry(page))
                page.mouse.move(2,2);expect(preview).to_be_hidden()
                assert not errors,errors
                browser.close()
            print('PASS annotation pixels and names, per-screenshot/pane scope, original toggle, hover handoff, keyboard focus/Escape, DPR 2 and unchanged evidence/layout')
        finally:server.shutdown();server.server_close();worker.join(timeout=3)


if __name__=='__main__':main()
