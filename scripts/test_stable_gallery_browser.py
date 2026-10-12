#!/usr/bin/env python3
"""Verify fixed gallery geometry and transient previews on an isolated real viewer."""
from pathlib import Path
import sys,tempfile,threading
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests')]
from server import make_server
from test_object_double_click_browser import model
from test_prompt_drag_browser import image_data
from workspace_ui_helpers import control
from playwright.sync_api import sync_playwright,expect

HOOK='\nwindow.__galleryCheck={state,camera,controls,refreshWorkspace,renderSceneSnapshots};'

def ready(page):
    page.wait_for_function('window.__galleryCheck?.state.workspaceReady && !__galleryCheck.state.sceneLoading && !__galleryCheck.state.seeking')
    page.wait_for_timeout(500)

def evidence(page):
    return page.evaluate('JSON.stringify({snapshots:__galleryCheck.state.sceneSnapshots,dynamic:__galleryCheck.state.dynamicSnapshots,annotations:__galleryCheck.state.annotations,active:__galleryCheck.state.snapshot?.id,time:__galleryCheck.state.time,camera:__galleryCheck.camera.matrixWorld.toArray()})')

def geometry(page):
    return page.evaluate("['scene-snapshots','scene-stage','reference-stage','chat-dock'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.x,r.y,r.width,r.height]})")

def stable(before,after):
    assert all(abs(a-b)<1 for row1,row2 in zip(before,after) for a,b in zip(row1,row2)),(before,after)

def main():
    with tempfile.TemporaryDirectory(prefix='stable-gallery-') as directory:
        root=Path(directory);project=root/'project';project.mkdir()
        server=make_server(port=0,project_dir=project,data_dir=root/'data',web_dir=ROOT/'web',enable_codex=False)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        store=server.scene_store;session=server.workspace_gateway.ensure()['session_id']
        asset=root/'scene.glb';model(asset);store.import_model(str(asset),object_id='test_model',name='柜子')
        errors=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                context=browser.new_context(viewport={'width':1440,'height':900})
                context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='text/javascript',body=(ROOT/'web/app.js').read_text()+HOOK))
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                url=server.browser_url(session);page.goto(url);ready(page)
                for _ in range(3):
                    control(page,'#capture-scene-button').click();control(page,'#scene-live-card').click()
                live=page.locator('#scene-live-card');card=page.locator('.snapshot-open[data-snapshot-id]').first
                tooltip=page.locator('#snapshot-card-preview')
                page.mouse.move(2,2);ready(page);before=geometry(page);saved=evidence(page)
                live.hover();page.wait_for_timeout(650);expect(tooltip).to_be_hidden();stable(before,geometry(page))
                expect(live.locator('.snapshot-source')).to_have_count(0)
                card.hover();page.wait_for_timeout(100);page.mouse.move(2,2);page.wait_for_timeout(500);expect(tooltip).to_be_hidden()
                card.hover();expect(tooltip).to_be_visible();page.wait_for_timeout(180);stable(before,geometry(page));assert evidence(page)==saved
                chat=page.locator('#chat-dock').bounding_box();tip=tooltip.bounding_box()
                assert tip['x']+tip['width']<=chat['x'] or tip['x']>=chat['x']+chat['width'] or tip['y']+tip['height']<=chat['y']
                page.screenshot(path='/tmp/stable-gallery-light.png')
                page.keyboard.press('Escape');expect(tooltip).to_be_hidden();stable(before,geometry(page))
                page.mouse.move(2,2);card.hover();expect(tooltip).to_be_visible();card.click();expect(tooltip).to_be_hidden()
                assert page.evaluate('__galleryCheck.state.snapshot.id')==card.get_attribute('data-snapshot-id')
                page.mouse.move(2,2);card.focus();card.press('ArrowRight');expect(tooltip).to_be_visible();page.keyboard.press('Escape')
                toggle=page.locator('#snapshot-gallery-toggle');x=toggle.bounding_box()['x']
                toggle.click();page.wait_for_timeout(450);expect(toggle).to_have_attribute('aria-expanded','false')
                expect(page.locator('#snapshot-gallery-count')).to_have_text('3 张')
                assert page.locator('#scene-snapshots').bounding_box()['height']<45
                assert abs(toggle.bounding_box()['x']-x)<1
                page.reload();ready(page);expect(toggle).to_have_attribute('aria-expanded','false')
                toggle.click();page.wait_for_timeout(450);expect(toggle).to_have_attribute('aria-expanded','true')
                for layout in ['immersive','compare']:
                    control(page,'#immersive-toggle' if layout=='immersive' else '#comparison-layout-button').click();ready(page)
                    control(page,'#theme-toggle').click();ready(page)
                    before=geometry(page);page.mouse.move(2,2);card.hover();expect(tooltip).to_be_visible();page.wait_for_timeout(180);stable(before,geometry(page))
                    tip=tooltip.bounding_box();assert tip['x']>=0 and tip['y']>=0 and tip['x']+tip['width']<=1440
                    page.screenshot(path='/tmp/stable-gallery-'+layout+'.png');page.mouse.move(2,2)
                # A real video moment still opens at its saved time; time quoting and deletion survive.
                frames=[{'name':f'frame{i}.png','data_url':image_data('navy'),'time_sec':i*.5} for i in range(3)]
                store.set_reference_clip(session,{'name':'参考视频','fps':2,'frames':frames})
                page.reload();ready(page);control(page,'#scene-live-card').click();control(page,'#capture-scene-button').click();ready(page)
                moment=page.locator('.snapshot-card[data-kind=moment]').first
                before=geometry(page);saved=evidence(page)
                moment.locator('.snapshot-open').hover();expect(tooltip).to_be_visible();stable(before,geometry(page));assert evidence(page)==saved
                moment.locator('.snapshot-time-reference').dblclick();expect(tooltip).to_be_hidden()
                assert page.locator('#feedback-note').input_value()
                moment.locator('.snapshot-remove').click();expect(page.locator('.snapshot-card[data-kind=moment]')).to_have_count(0)
                page.keyboard.press('Control+z');expect(page.locator('.snapshot-card[data-kind=moment]')).to_have_count(1)
                # Hover doesn't create a popup on touch, and reduced-motion doesn't leave animations running.
                page.emulate_media(reduced_motion='reduce');page.mouse.move(2,2);card.hover();expect(tooltip).to_be_visible()
                assert tooltip.evaluate('el=>el.getAnimations().every(a=>a.playState!=="running")')
                page.keyboard.press('Escape');expect(tooltip).to_be_hidden()
                page.set_viewport_size({'width':390,'height':844});control(page,'#immersive-toggle').click();ready(page)
                card.hover();page.wait_for_timeout(650)
                if tooltip.is_visible():
                    tip=tooltip.bounding_box();assert tip['x']>=0 and tip['x']+tip['width']<=390
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path='/tmp/stable-gallery-narrow.png')
                control(page,'#chat-collapse').click();page.wait_for_timeout(500)
                live.focus();live.press('End');expect(tooltip).to_be_visible()
                page.locator('#scene-snapshot-strip').dispatch_event('wheel',{'deltaX':80});expect(tooltip).to_be_hidden()

                touch=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                t=touch.new_page();t.goto(url);t.wait_for_selector('#scene-live-card');t.locator('#scene-live-card').tap();t.wait_for_timeout(650)
                expect(t.locator('#snapshot-card-preview')).to_be_hidden();touch.close()
                assert not errors,errors
                browser.close()
            print('PASS fixed geometry, delayed/cancelled preview, chat avoidance, keyboard/Escape, explicit collapse/reload, both layouts/themes, video time/delete/undo, reduced motion and narrow/touch')
        finally:server.shutdown();server.server_close();worker.join(timeout=3)
if __name__=='__main__':main()
