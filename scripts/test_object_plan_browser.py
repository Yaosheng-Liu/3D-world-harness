#!/usr/bin/env python3
"""Browser regression for plan evidence UI; fixtures are explicitly synthetic.

Exercises the actual workflow module with a missed object, hierarchy, existing
review and published objects. No model is called and no scene is published.
"""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='object-plan-browser-'))
    (directory / 'web').symlink_to(ROOT / 'web', target_is_directory=True)
    for name, color in [('reference', '#c7b99b'), ('mask', '#40aa85'), ('render', '#7fa4bb')]:
        image = Image.new('RGB', (320, 200), '#eee8df')
        draw = ImageDraw.Draw(image)
        draw.rectangle((50, 40, 210, 160), fill=color)
        draw.rectangle((245, 90, 270, 165), fill='#4a5860')
        image.save(directory / f'{name}.png')
    planned = [
        {'id': 'room', 'name': '房间结构', 'parent_id': None, 'kind': 'structure',
         'segmentation_status': 'not_required', 'geometry_status': 'not_required',
         'notes': '合成测试分组。', 'evidence': []},
        {'id': 'chair', 'name': '独立椅子', 'parent_id': 'room', 'kind': 'object',
         'segmentation_status': 'segmented', 'geometry_status': 'ready',
         'notes': '合成测试已有掩码。', 'evidence': [{'view_id': '12', 'label': '第 12 帧', 'has_mask': True}, {'view_id': '24', 'label': '第 24 帧', 'has_mask': True}]},
        {'id': 'lamp', 'name': '漏检台灯', 'parent_id': None, 'kind': 'object',
         'segmentation_status': 'missing', 'geometry_status': 'insufficient_evidence',
         'notes': '原图可见，分割暂未检出，必须保留。', 'evidence': [{'view_id': '24', 'label': '第 24 帧', 'has_mask': False}]},
    ]
    fixture = {
        'status': {'stage': 'needs_revision', 'candidate_id': 'older-candidate', 'object_count': 1,
                   'accepted': False, 'round': 1, 'max_rounds': 3,
                   'builder': {'model': 'gpt-6-astra', 'effort': 'high'},
                   'reviewer': {'model': 'gpt-6.1-sol', 'effort': 'ultra'},
                   'summary': '上次审核记录（合成测试）。',
                   'views': [{'id': 'view_0'}, {'id': 'source_24', 'reference_only': True}],
                   'issues': [{'id': 'missing-lamp', 'object_id': 'scene', 'view_id': 'source_24',
                               'category': 'inventory', 'severity': 'major', 'region': [.7, .4, .9, .9],
                               'evidence': '原图中发现清单外物体（合成测试）。', 'acceptance_criterion': '补充物体清单'}],
                   'planning': {'stage': 'ready_for_modeling', 'run_id': 'synthetic-plan',
                                'updated_at': '2026-10-25T01:00:00Z',
                                'planner': {'model': 'gpt-6-astra', 'effort': 'high'},
                                'summary': '合成浏览器测试，不代表实际重建已完成。',
                                'completed_stages': ['reference', 'planning', 'segmenting', 'geometry'],
                                'objects': planned,
                                'unresolved': [{'object_id': 'lamp', 'reason': '台灯漏检', 'next_action': '建模 Agent 对照原图补建台灯，并核对物体边界'}]}},
        'editor': {'revision': 2, 'scene_revision': 3, 'units': 'arbitrary', 'matches_display': True,
                   'objects': [{'id': 'chair', 'name': '已发布椅子', 'parent_id': None,
                                'position': [0, 0, 0], 'rotation': [0, 0, 0],
                                'evidence': {'notes': '合成已发布对象。'}}]},
        'workspace': {'scene_revision': 3, 'approvals': [], 'reference_clip': {'frames': [{}, {}]}}
    }
    (directory / 'fixture.json').write_text(json.dumps(fixture, ensure_ascii=False))
    (directory / 'fixture.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>计划 UI 合成回归</title>
<link rel="stylesheet" href="/web/reconstruction-workflow.css"><style>
body{font-family:system-ui,sans-serif;margin:20px;color:#202423;background:#f8f8f4}button,select,input{font:inherit}button{cursor:pointer}button:disabled{cursor:default;opacity:.5}*{box-sizing:border-box}button{padding:6px 10px;border:1px solid #ccc;background:#fafaf7;border-radius:5px}.pane-head{display:flex;flex-wrap:wrap}h3{font-size:17px}p{line-height:1.5}
</style><main class="scene-pane"><div class="pane-head"></div></main><script type="module">
import {createReconstructionWorkflow} from '/web/reconstruction-workflow.js';
const fixture=await (await fetch('/fixture.json')).json(); window.fixture=fixture;
window.focused=[];window.saved=[];
const api=async(path, options)=>{
  if(path==='/api/workspace/reconstruction') return structuredClone(fixture.status);
  if(path==='/api/workspace/world-editor' && options?.method==='POST'){
    window.saved.push(options.body);fixture.editor.revision++;fixture.editor.scene_revision++;
    Object.assign(fixture.editor.objects[0],{position:options.body.position,rotation:options.body.rotation});
    return {scene:{revision:fixture.editor.scene_revision}};
  }
  if(path==='/api/workspace/world-editor')return structuredClone(fixture.editor);
  throw Error('Unexpected API '+path);
};
window.workflow=createReconstructionWorkflow({api,resourceURL:path=>path,loadScene:async()=>{},focusObject:id=>window.focused.push(id),alignView:()=>{}});
window.updateStage=async stage=>{if(stage===null)fixture.status.planning=null;else fixture.status.planning.stage=stage;await window.workflow.refresh(fixture.workspace,true);};
await window.workflow.refresh(fixture.workspace,true);window.ready=true;
</script></html>''')
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in {'/api/workspace/object-plan/image', '/api/workspace/reconstruction/image'}:
                query = parse_qs(parsed.query)
                requests.append({'endpoint': parsed.path, **{k: v[0] for k, v in query.items()}})
                self.path = '/' + query['kind'][0] + '.png'
            return super().do_GET()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--no-proxy-server'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/fixture.html')
            page.wait_for_function('window.ready')
            page.locator('#flow-open').click()
            page.wait_for_function("document.querySelector('#flow-object').options.length === 1")
            assert page.locator('#flow-plan-list button').count() == 3
            assert page.locator('#flow-plan-list button').nth(1).get_attribute('data-object-id') == 'chair'
            assert '属于 room' in page.locator('[data-object-id=chair]').inner_text()
            assert '初始证据可用，等待精细建模' in page.locator('#flow-stage').inner_text()
            assert '计划 / SAM / DA3 / TSDF' in page.locator('.flow-steps [data-stage=initialization]').inner_text()
            assert '建模 Agent 返工' in page.locator('.flow-review-loop').inner_text()
            assert '初始证据不代表最终还原质量' in page.locator('#flow-plan-counts').inner_text()
            assert '已有审核记录' in page.locator('#flow-candidate').inner_text()
            assert not page.locator('.flow-steps [data-stage=accepted]').evaluate("e=>e.classList.contains('done')")
            page.locator('#flow-issues button').click()
            assert page.locator('#flow-view').input_value() == 'source_24'
            assert page.locator('#flow-render').locator('..').locator('..').is_hidden()
            assert page.locator('#flow-align').is_disabled()
            assert '物体清单' in page.locator('#flow-issues').inner_text()
            assert '交给建模 Agent' in page.locator('#flow-issues').inner_text()
            assert '返回计划' not in page.locator('#flow-issues').inner_text()
            # Current categories take precedence; historical target records stay readable.
            page.evaluate("async()=>{const issue=window.fixture.status.issues[0];issue.category='appearance';issue.target='planning';await window.workflow.refresh(window.fixture.workspace,true);}")
            assert '外观与材质' in page.locator('#flow-issues').inner_text()
            assert '返回计划' not in page.locator('#flow-issues').inner_text()
            page.evaluate("async()=>{const issue=window.fixture.status.issues[0];delete issue.category;issue.target='segmentation';await window.workflow.refresh(window.fixture.workspace,true);}")
            assert '分割与归属' in page.locator('#flow-issues').inner_text()
            assert '交给建模 Agent' in page.locator('#flow-issues').inner_text()
            assert '修正分割' not in page.locator('#flow-issues').inner_text()
            page.locator('#flow-view').select_option('view_0')
            assert page.locator('#flow-render').locator('..').locator('..').is_visible()
            page.locator('[data-object-id=chair]').click()
            page.wait_for_function("document.querySelector('#flow-plan-mask').naturalWidth > 0")
            assert page.locator('#flow-plan-focus').is_enabled()
            page.locator('#flow-plan-view').select_option('24')
            page.wait_for_function("document.querySelector('#flow-plan-mask').complete && document.querySelector('#flow-plan-mask').naturalWidth > 0")
            page.screenshot(path=str(args.output / 'plan-evidence.png'))
            page.locator('#flow-plan-focus').click()
            assert page.evaluate('window.focused') == ['chair']
            page.locator('#flow-open').click()
            page.locator('#flow-unresolved button').click()
            assert '漏检台灯' in page.locator('#flow-plan-object-name').inner_text()
            assert page.locator('#flow-plan-mask-figure').is_hidden()
            assert page.locator('#flow-plan-focus').is_disabled()
            assert '物体仍保留' in page.locator('#flow-plan-image-status').inner_text()
            assert page.locator('#flow-plan-list button').count() == 3
            page.screenshot(path=str(args.output / 'missing-object.png'))
            page.evaluate("window.updateStage('needs_revision')")
            assert '建模 Agent 返工' in page.locator('#flow-stage').inner_text()
            assert page.locator('.flow-steps [data-stage=modeling]').evaluate("e=>e.classList.contains('current')")
            assert not page.locator('.flow-steps [data-stage=initialization]').evaluate("e=>e.classList.contains('current')")
            page.evaluate("window.updateStage('segmenting')")
            assert page.locator('.flow-steps [data-stage=initialization]').evaluate("e=>e.classList.contains('current')")
            assert page.locator('#flow-apply').is_disabled()
            page.evaluate("window.updateStage('ready_for_modeling')")
            assert page.locator('#flow-apply').is_enabled()
            page.locator('#flow-position input').nth(0).fill('0.25')
            page.locator('#flow-apply').click()
            page.wait_for_function("document.querySelector('#flow-edit-result').textContent.startsWith('已保存')")
            assert page.evaluate('window.saved[0].position[0]') == .25
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.locator('#reconstruction-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth+2')
            page.screenshot(path=str(args.output / 'mobile.png'))
            page.evaluate('window.updateStage(null)')
            assert page.locator('#flow-plan').is_hidden()
            assert page.locator('#flow-object').is_visible()
            assert '待改进' in page.locator('#flow-stage').inner_text()
            assert not errors, errors
            browser.close()
        assert any(item.get('object_id') == 'chair' and item.get('view_id') == '24' and item.get('kind') == 'mask' for item in requests)
        assert any(item.get('object_id') == 'lamp' and item.get('kind') == 'reference' for item in requests)
        assert not any(item.get('object_id') == 'lamp' and item.get('kind') == 'mask' for item in requests)
        result = {'synthetic_only': True, 'missing_object_retained': True, 'hierarchy_visible': True,
                  'mask_view_selection': True, 'unpublished_object_not_editable': True,
                  'previous_review_separate': True, 'existing_editor_preserved': True,
                  'active_plan_blocks_edit': True, 'mobile_no_overflow': True,
                  'legacy_without_plan': True, 'source_only_review_issue': True, 'initialization_only': True,
                  'all_review_feedback_to_modeler': True, 'legacy_issue_category': True, 'page_errors': errors}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
