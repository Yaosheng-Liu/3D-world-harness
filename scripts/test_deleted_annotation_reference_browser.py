#!/usr/bin/env python3
"""Identify deleted mark citations without losing native prompt or mark undo."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests')]
from gateway import WorkspaceGateway
from server import make_server
from test_object_double_click_browser import model
from test_folder_import_browser import png
from workspace_ui_helpers import choose_tool,control


def ready(page):
    page.wait_for_function('window.__deletedRefs?.state.workspaceReady && !__deletedRefs.state.sceneLoading && '
        '!__deletedRefs.state.submitting && document.getElementById("reference-image").naturalWidth>0')


def marks(page):
    return page.evaluate('structuredClone(__deletedRefs.state.annotations)')


def draw_three(page):
    choose_tool(page,'line','reference')
    canvas=page.locator('#reference-annotations');box=canvas.bounding_box();assert box
    before=len(marks(page))
    for fraction in (.22,.45,.68):
        x=box['x']+box['width']*fraction
        page.mouse.move(x,box['y']+box['height']*.3);page.mouse.down()
        page.mouse.move(x+25,box['y']+box['height']*.49,steps=6);page.mouse.up()
    page.wait_for_function('size=>__deletedRefs.state.annotations.length===size',arg=before+3)
    return marks(page)[before:]


def open_list(page):
    if not page.locator('#references-dialog').evaluate('el=>el.open'):
        control(page,'#references-dialog-button').click()


def close_list(page):
    page.locator('[data-close-dialog="references-dialog"]').click()


def quote_marks(page,text):
    open_list(page);page.locator('#reference-all-annotations').click()
    page.wait_for_function('!document.getElementById("references-dialog").open')
    field=page.locator('#feedback-note');field.fill(text)
    assert all(alias in field.input_value() for alias in ('📍1','📍2','📍3'))


def remove_marks(page,entries):
    open_list(page)
    for entry in entries:
        item=page.locator('.annotation-item').filter(has=page.locator('.annotation-select[data-annotation-id="'+entry['id']+'"]'))
        item.locator('.annotation-remove').click()
    close_list(page)


def fail_send(page,missing,text,first_alias='📍2'):
    page.locator('#submit-button').click()
    toast=page.locator('#toast').inner_text()
    for alias,entry in missing:
        assert alias in toast and entry['name'] in toast,('error must identify exact alias and original mark name',toast,alias,entry['name'])
    field=page.locator('#feedback-note')
    assert field.input_value()==text
    assert field.evaluate('el=>document.activeElement===el')
    assert field.evaluate('el=>el.value.slice(el.selectionStart,el.selectionEnd)')==first_alias
    assert page.evaluate('__deletedRefs.state.pendingSubmission===null && !__deletedRefs.state.submitting')
    return toast


def send(page):
    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/feedback')) as response:
        page.locator('#submit-button').click()
    assert response.value.status==201,response.value.text()
    page.wait_for_function('!__deletedRefs.state.submitting && __deletedRefs.state.annotations.length===0')
    assert page.locator('#feedback-note').input_value()==''
    return response.value.json()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable',default='/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='deleted-annotation-reference-') as temporary:
        root=Path(temporary);project=root/'project';project.mkdir()
        model(project/'model.glb');png(project/'gt.png','navy')
        errors=[];submissions=[]
        with patch.object(WorkspaceGateway,'create_target',side_effect=AssertionError('reference validation created a task')) as tasks, \
             patch.object(WorkspaceGateway,'list_models',side_effect=AssertionError('reference validation queried models')) as models:
            server=make_server(port=0,data_dir=root/'data',project_dir=project,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            store=server.scene_store;session=server.workspace_gateway.ensure()['session_id']
            store.import_model(str(project/'model.glb'),object_id='model',name='Current model')
            server.workspace_gateway.add_reference_paths([str(project/'gt.png')])
            original_scene=copy.deepcopy(store.scene())
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True,executable_path=args.browser_executable,args=[
                        '--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    hook='\nwindow.__deletedRefs={state,ui,promptText,promptReferenceText};'
                    context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('request',lambda call:submissions.append(call.post_data_json) if call.method=='POST' and call.url.endswith('/feedback') else None)
                    page.goto(server.browser_url(session));ready(page)
                    first=draw_three(page)
                    text='请按 📍1、📍2 和 📍3 调整；这句备注保留。'
                    quote_marks(page,text);canonical=page.evaluate('__deletedRefs.promptText()')
                    remove_marks(page,[first[1]])
                    invalid=page.locator('.prompt-reference-chip[data-reference-state="deleted"]')
                    assert invalid.count()==1 and '📍2' in invalid.inner_text()
                    assert first[1]['name'] in invalid.locator('.prompt-reference-preview').get_attribute('title')
                    fail_send(page,[('📍2',first[1])],text)
                    assert len(submissions)==0 and not store.state['feedback']
                    assert marks(page)==[first[0],first[2]]
                    field=page.locator('#feedback-note');field.press('Delete')
                    assert '📍2' not in field.input_value() and '📍1' in field.input_value() and '📍3' in field.input_value()
                    field.press('Control+z')
                    assert field.input_value()==text and page.evaluate('__deletedRefs.promptText()')==canonical
                    control(page,'#undo-annotation').click()
                    assert marks(page)==first and field.input_value()==text
                    assert page.locator('.prompt-reference-chip.is-invalid').count()==0
                    saved=send(page)
                    assert saved['note']==canonical and [entry['id'] for entry in saved['annotations']]==[entry['id'] for entry in first]
                    print('PASS real drawn marks: deleting the middle mark names/selects 📍2 and its original name before any POST; native text undo and annotation undo preserve the exact draft, then valid feedback sends all three marks',flush=True)

                    second=draw_three(page)
                    text='先按 📍1，再看 📍2 和 📍3，重复对照 📍2；保留此备注。'
                    quote_marks(page,text);canonical=page.evaluate('__deletedRefs.promptText()')
                    remove_marks(page,[second[1],second[2]])
                    message=fail_send(page,[('📍2',second[1]),('📍3',second[2])],text)
                    assert message.count('📍2')==1 and message.count('📍3')==1,message
                    assert len(submissions)==1 and marks(page)==[second[0]]
                    page.reload();ready(page)
                    assert page.locator('#feedback-note').input_value()==text
                    assert page.locator('.prompt-reference-chip[data-reference-state="deleted"]').count()==2
                    message=fail_send(page,[('📍2',second[1]),('📍3',second[2])],text)
                    assert message.count('📍2')==1 and message.count('📍3')==1,message
                    assert page.evaluate('__deletedRefs.promptText()')==canonical
                    assert len(submissions)==1
                    print('PASS one attempt lists every distinct invalid citation, repeated alias is not duplicated, and refreshed deleted drafts retain original mark names and select the first missing source',flush=True)

                    for alias in ('📍2','📍3'):
                        page.locator('.prompt-reference-chip').filter(has_text=alias).locator('.prompt-reference-remove').click()
                    current=page.locator('#feedback-note').input_value()
                    assert '📍1' in current and '📍2' not in current and '📍3' not in current and '保留此备注' in current
                    last=send(page)
                    assert len(last['annotations'])==1 and last['annotations'][0]['id']==second[0]['id']
                    assert second[1]['id'] not in last['note'] and second[2]['id'] not in last['note']
                    assert second[0]['id'] in last['note'] and '保留此备注' in last['note']
                    assert store.scene()==original_scene and not errors,errors
                    tasks.assert_not_called();models.assert_not_called()
                    print('PASS removing only the invalid prompt citations sends the remaining valid mark and prose; source scene/task unchanged, no browser errors or model calls',flush=True)
                    browser.close()
            finally:server.shutdown();thread.join(5);server.server_close()
    print('ALL DELETED ANNOTATION REFERENCE CHECKS PASSED',flush=True)


if __name__=='__main__':main()
