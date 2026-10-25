#!/usr/bin/env python3
"""Synthetic browser checks for stage navigation, history isolation and current editing."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='history-browser-') as temp:
        root = Path(temp); (root / 'web').symlink_to(ROOT / 'web', target_is_directory=True)
        for name, color in [('reference', '#7190a0'), ('first', '#b3bdad'), ('second', '#b0a176'), ('mask', '#45956c')]:
            Image.new('RGB', (480, 270), color).save(root / (name + '.png'))
        plan = {'stage':'modeled','run_id':'run-one','objects':[
            {'id':'chair','name':'椅子','parent_id':None,'kind':'object','segmentation_status':'segmented','geometry_status':'ready','notes':'原始证据','evidence':[{'view_id':'1','has_mask':True}]},
            {'id':'lamp','name':'台灯','parent_id':None,'kind':'object','segmentation_status':'missing','geometry_status':'insufficient_evidence','notes':'需要补建','evidence':[{'view_id':'1','has_mask':False}]}],
            'unresolved':[{'object_id':'lamp','reason':'未检出','next_action':'依据原图建模'}]}
        def record(key, stage, title, image=None):
            return {'id':key,'stage':stage,'title':title,'run_id':'run-one','recorded_at':'2026-10-26T01:00:00Z','summary':'合成测试记录',
                    'facts':[['对象','2']],'views':[{'id':'front','reference':{'url':'/reference.png'},'render':{'url':'/'+image+'.png'}}] if image else [],'documents':[], 'items':[], 'issues':[]}
        entries = [record('init','initialization','初始场景','first'),record('plan','initialization','物体计划'),record('first','modeling','第 1 轮 · 首次建模','first'),record('second','modeling','第 2 轮 · 反馈返修','second'),record('review','reviewing','第 1 轮审核','first')]
        entries[1]['kind']='plan'
        entries[-1].update(verdict='revise',dimensions={'structure':'failed'},issues=[
            {'id':'issue-'+kind,'object_id':'chair','view_id':'front','category':kind,'region':[.1,.2,.5,.7],'severity':'major','evidence':'此类别的具体缺陷','acceptance_criterion':'在原视角复验'}
            for kind in ['spatial_structure','silhouette','texture_alignment','lighting']])
        fixture={'history':{'entries':entries,'errors':[]},'plan':plan,'status':{'stage':'needs_revision','planning':plan,'updated_at':'one','accepted':False},
                 'workspace':{'scene_revision':4,'approvals':[],'reference_clip':{'frames':[{'url':'/reference.png'}]}},
                 'editor':{'revision':2,'scene_revision':4,'units':'arbitrary','matches_display':True,'editable':True,'objects':[{'id':'chair','name':'椅子','parent_id':None,'position':[0,0,0],'rotation':[0,0,0],'evidence':{'notes':'合成测试'}}]}}
        (root / 'fixture.json').write_text(json.dumps(fixture,ensure_ascii=False))
        (root / 'fixture.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>阶段回溯合成测试</title>
<link rel="stylesheet" href="/web/styles.css"><link rel="stylesheet" href="/web/reconstruction-workflow.css"><main class="scene-pane" style="padding:24px"><div class="pane-head"></div></main><script type="module">
import {createReconstructionWorkflow} from '/web/reconstruction-workflow.js';
window.fixture=await(await fetch('/fixture.json')).json();window.writes=[];window.focused=[];window.loaded=[];
const api=async(path,options)=>{if(path==='/api/workspace/reconstruction')return structuredClone(fixture.status);if(path==='/api/workspace/reconstruction/history')return structuredClone(fixture.history);if(path.startsWith('/api/workspace/object-plan?'))return structuredClone(fixture.plan);if(path==='/api/workspace/world-editor'){if(options?.method==='POST'){writes.push(options.body);fixture.editor.revision++;fixture.editor.scene_revision++;return {scene:{revision:fixture.editor.scene_revision}};}return structuredClone(fixture.editor);}throw Error('Unexpected '+path);};
window.workflow=createReconstructionWorkflow({api,resourceURL:p=>p,loadScene:async s=>loaded.push(s.revision),focusObject:id=>focused.push(id),alignView:()=>{}});await workflow.refresh(fixture.workspace,true);window.ready=true;
</script></html>''')
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                parsed=urlsplit(self.path)
                if parsed.path=='/api/workspace/object-plan/image':
                    self.path='/mask.png' if 'kind=mask' in parsed.query else '/reference.png'
                return super().do_GET()
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(root)))
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        errors=[]
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--no-sandbox','--no-proxy-server'])
                page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}/fixture.html');page.wait_for_function('window.ready')
                assert page.locator('.flow-track button').count()==6
                page.locator('.flow-track [data-stage=modeling]').click()
                page.wait_for_function("document.querySelector('#flow-title').textContent.includes('第 2 轮')")
                assert page.locator('#flow-plan').is_hidden() and page.locator('#flow-edit-form').is_hidden()
                assert page.locator('#flow-render').get_attribute('src')=='/second.png'
                page.locator('#flow-history-version').select_option('first')
                assert page.locator('#flow-render').get_attribute('src')=='/first.png'
                page.locator('.flow-steps [data-stage=reviewing]').click()
                page.wait_for_selector('#flow-issues button')
                assert page.locator('#flow-issues button').count()==4
                for key,label in [('spatial_structure','空间结构错误'),('silhouette','轮廓错误'),('texture_alignment','纹理错位'),('lighting','光照偏差')]:
                    page.locator('#flow-issue-filter').select_option(key)
                    assert page.locator('#flow-issues button').count()==1
                    assert label in page.locator('#flow-issue-detail').inner_text()
                    page.locator('#flow-issue-detail button').click()
                    assert page.locator('#flow-reference').locator('..').locator('.flow-region').is_visible()
                page.screenshot(path=str(args.output/'review-desktop.png'))
                page.locator('.flow-steps [data-stage=initialization]').click()
                page.locator('#flow-history-version').select_option('plan')
                page.wait_for_selector('#flow-plan-list button')
                assert page.locator('#flow-plan-list button').count()==2
                page.locator('[data-object-id=lamp]').click()
                assert page.locator('#flow-plan-mask-figure').is_hidden()
                assert page.locator('#flow-plan-focus').is_disabled()
                page.locator('#flow-plan-search').fill('椅子')
                assert page.locator('#flow-plan-list button:visible').count()==1
                page.locator('[data-object-id=chair]').click()
                page.wait_for_function("document.querySelector('#flow-plan-mask').naturalWidth>0")
                page.screenshot(path=str(args.output/'initialization-desktop.png'))
                page.locator('#flow-history-version').select_option('init')
                assert page.locator('#flow-plan').is_hidden()
                assert page.locator('#flow-render').get_attribute('src')=='/first.png'
                page.locator('.flow-steps [data-stage=accepted]').click()
                assert '尚未验收' in page.locator('#flow-empty').inner_text()
                assert page.evaluate('writes.length===0 && loaded.length===0')
                page.locator('#flow-close').click()
                page.locator('#flow-editor-open').click();page.wait_for_selector('#flow-object')
                assert not page.locator('#reconstruction-dialog').is_visible()
                page.get_by_label('位置 X',exact=True).fill('2.5');page.locator('#flow-apply').click()
                page.wait_for_function('writes.length===1 && loaded.length===1')
                assert page.evaluate('writes[0].position[0]')==2.5
                page.locator('#flow-editor-close').click()
                page.evaluate("fixture.editor.draft=true;fixture.editor.editable=false")
                page.locator('#flow-editor-open').click();page.wait_for_function("document.querySelector('#flow-edit-form').hidden")
                page.locator('#flow-focus').click();assert page.evaluate('focused')==['chair']
                page.set_viewport_size({'width':390,'height':844})
                page.locator('.flow-track [data-stage=reviewing]').click();page.wait_for_selector('#flow-issues button')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('.flow-content').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                page.screenshot(path=str(args.output/'review-mobile.png'))
                page.keyboard.press('Escape');assert not page.locator('#reconstruction-dialog').is_visible()
                assert not errors,errors
                result={'synthetic_only':True,'stage_navigation':True,'history_versions_isolated':True,'classified_review_evidence':True,'initialization_evidence':True,'current_editor_separate':True,'history_never_writes_scene':True,'draft_stays_readonly':True,'mobile_no_overflow':True,'page_errors':errors}
                (args.output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2));browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':main()
