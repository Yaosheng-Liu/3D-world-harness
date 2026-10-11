#!/usr/bin/env python3
"""Capture immutable scene examples through a real workbench and feedback API."""

from __future__ import annotations

import argparse
import base64
import copy
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests')]

from feedback_summary import model_input_plan
from gateway import WorkspaceGateway
from mcp_server import _visual_tool_result
from server import make_server
from test_folder_import_browser import fixtures,http,png,source_files
from test_object_double_click_browser import model
from test_selection_outline_browser import model as animated_model
from test_scene_unload_browser import ready,draft,sidebar,row,request


def import_scenes(page,path):
    sidebar(page)
    page.locator('#sidebar-open-folder').click()
    # The page initially browses its suggested folder. Let that request finish
    # before supplying the test's destination, as a person would do.
    page.wait_for_function('!document.getElementById("folder-import-browse").disabled')
    field=page.locator('#folder-import-path');field.fill(str(path))
    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/api/projects/import-folder')) as captured:
        field.press('Enter')
    assert captured.value.status==200,(captured.value.status,captured.value.text())
    result=captured.value.json()
    page.locator('#sidebar-folder-import [data-sidebar-home]').click()
    page.wait_for_function('!__unloadCheck.state.loadingProjects')
    return result


def cite(page,project_id):
    sidebar(page)
    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/api/workspace/scene-references')) as captured:
        row(page,project_id).locator('.project-cite-trigger').click()
    assert captured.value.status==201,(captured.value.status,captured.value.text())
    record=captured.value.json()
    page.wait_for_function('__sceneCheck.state.citingSceneProject===null && !document.getElementById("projects-dialog").open')
    assert page.evaluate('id=>__sceneCheck.state.sceneRefs.some(entry=>entry.id===id)',record['id'])
    return record


def snapshot(page):
    return page.evaluate("""JSON.stringify({camera:__sceneCheck.cameraData(),revision:__sceneCheck.state.sceneRevision,
        scene:__sceneCheck.state.sceneObjects,selected:__sceneCheck.state.selectedId,node:__sceneCheck.state.selectedSceneNode,
        panes:__sceneCheck.state.paneModes,time:__sceneCheck.state.time,task:__sceneCheck.state.boundThreadId,
        nodes:[...__sceneCheck.state.objectNodes.values()].map(node=>[node.uuid,node.userData.gltfRoot?.uuid]),
        animation:[...__sceneCheck.state.animations.values()].map(entry=>entry.mixer.time)})""")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    cached=Path('/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    parser.add_argument('--browser-executable',default=str(cached) if cached.is_file() else None)
    args=parser.parse_args()
    from playwright.sync_api import expect,sync_playwright

    with tempfile.TemporaryDirectory(prefix='scene-reference-browser-') as temporary:
        root=Path(temporary);original=root/'original';original.mkdir()
        model(original/'original.glb');png(original/'original.png','gray')
        dynamic,static,_=fixtures(root/'examples')
        animated_model(dynamic/'dynamic_scene.glb')
        source_before=source_files(root/'examples')
        errors=[];captures=[];writes=[]
        with patch.object(WorkspaceGateway,'create_target',side_effect=AssertionError('scene citation created a task')) as targets, \
             patch.object(WorkspaceGateway,'list_models',side_effect=AssertionError('scene citation queried models')) as models:
            server=make_server(port=0,data_dir=root/'data',project_dir=original,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            store=server.scene_store;gateway=server.workspace_gateway;registry=server.project_registry
            session=gateway.ensure()['session_id']
            store.import_model(str(original/'original.glb'),object_id='current_model',name='当前柜子')
            gateway.add_reference_paths([str(original/'original.png')])
            initial_scene=copy.deepcopy(store.scene());initial_session=copy.deepcopy(store.get_session(session))
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True,**({'executable_path':args.browser_executable} if args.browser_executable else {}),
                        args=['--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    hook='\nwindow.__unloadCheck={state,cameraData,renderer};window.__sceneCheck={state,cameraData,renderer,controls,promptText,promptReferenceText,getPromptMentionCandidates,setMode,selectObject,sceneReferenceStore,loadProjects};'
                    context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    def track(call):
                        if call.method not in {'GET','HEAD','OPTIONS'}:writes.append((call.method,call.url))
                        if call.method=='POST' and call.url.endswith('/api/workspace/scene-references'):captures.append(call.post_data_json)
                    page.on('request',track)
                    page.goto(server.browser_url(session));ready(page)
                    draft(page,'开头 后面')
                    imported=import_scenes(page,root/'examples')
                    ids={Path(item['project_dir']):item['project_id'] for item in imported['projects']}
                    dynamic_id,static_id=ids[dynamic],ids[static]
                    first_context=registry.get(dynamic_id);second_context=registry.get(static_id)
                    source_sessions=[copy.deepcopy(context.store.get_session(context.gateway.ensure()['session_id'])) for context in (first_context,second_context)]
                    page.locator('#close-projects').click();page.wait_for_function('!document.getElementById("projects-dialog").open')
                    page.wait_for_function('__sceneCheck.controls.transition===null')
                    page.evaluate("__sceneCheck.selectObject('current_model');__sceneCheck.setMode('arrow','reference');document.getElementById('feedback-note').setSelectionRange(2,2)")
                    before=snapshot(page);original_url=page.url
                    first=cite(page,dynamic_id);first_id=first['id']
                    expect(page.locator('#feedback-note')).to_have_value(re.compile('.*🏞️1.*',re.S))
                    assert page.locator('#feedback-note').input_value().startswith('开头') and page.locator('#feedback-note').input_value().endswith(' 后面')
                    assert page.evaluate('__sceneCheck.promptText()').count('[[scene:'+first_id+']]')==1
                    assert snapshot(page)==before and page.url==original_url
                    assert first['receiver_project_id']==registry.root.project_id and first['source_project_id']==dynamic_id
                    assert all(item['url'].startswith('/p/'+registry.root.project_id+'/assets/') for item in first['assets'])
                    assert first['source_reference_image']['url'].startswith('/p/'+registry.root.project_id+'/media/')
                    entry=page.evaluate('id=>__sceneCheck.state.sceneRefs.find(entry=>entry.id===id)',first_id)
                    image=Image.open(BytesIO(base64.b64decode(entry['preview_data_url'].split(',',1)[1]))).convert('RGB')
                    assert image.size==(640,400)
                    pixels=getattr(image,'get_flattened_data',image.getdata)()
                    assert sum(max(abs(pixel[index]-(234,233,227)[index]) for index in range(3))>15 for pixel in pixels)>1000
                    image.save('/tmp/scene_reference_preview_057.jpg')
                    assert store.scene()==initial_scene and store.get_session(session)==initial_session
                    print('PASS sidebar citation captures real model pixels and receiver-scoped assets/GT, inserts scene alias at the old caret and preserves live scene/camera/animation/selection/tool/task',flush=True)

                    second=cite(page,static_id);second_id=second['id']
                    expect(page.locator('#feedback-note')).to_have_value(re.compile('.*🏞️2.*',re.S))
                    repeated=cite(page,dynamic_id)
                    assert repeated['id']==first_id
                    assert page.evaluate('__sceneCheck.state.sceneRefs.length')==2
                    assert page.locator('#feedback-note').input_value().count('🏞️1')==2
                    assert page.evaluate('__sceneCheck.getPromptMentionCandidates().every(candidate=>["object","node","annotation","pose_edit"].includes(candidate.kind))')
                    assert snapshot(page)==before
                    chip=page.locator('.prompt-image-chip[data-scene-ref-id="'+first_id+'"]')
                    expect(chip).to_have_count(1)
                    chip.locator('.prompt-image-preview').click()
                    expect(page.locator('#prompt-reference-dialog')).to_be_visible()
                    expect(page.locator('#prompt-reference-detail')).to_contain_text(first['name'])
                    expect(page.locator('#prompt-reference-detail')).to_contain_text('版本 '+str(first['source_scene_revision']))
                    expect(page.locator('#prompt-reference-status')).to_contain_text('不会修改来源场景')
                    expect(page.locator('#prompt-scene-reference-preview')).to_be_visible()
                    page.screenshot(path='/tmp/scene_reference_ui_057.png')
                    page.mouse.move(30,30);expect(page.locator('#prompt-reference-dialog')).to_be_hidden()
                    print('PASS two scene aliases and repeated-source canonical dedup, small read-only preview closes on leave, @ remains restricted to the existing selected-object/mark candidates',flush=True)

                    # Save immutable evidence even after its source is edited and
                    # removed from the loaded catalog; then restore the draft.
                    awaitable=page.evaluate('__sceneCheck.sceneReferenceStore.pending.then(()=>true)');assert awaitable
                    source_revision=first_context.store.scene()['revision']
                    first_context.store.replace_scene(source_revision,[{'id':'later','name':'后来版本','type':'sphere','position':[3,0,0],'size':[.2,.2,.2]}])
                    status,_=request(base,'/api/projects/'+dynamic_id,method='DELETE',capability=store.browser_token)
                    assert status==200
                    status,restored=http(base,'/api/workspace/scene-references/'+first_id)
                    assert status==200 and restored['source_scene_revision']==source_revision
                    assert restored['scene']['objects']==first['scene']['objects']
                    page.reload();ready(page)
                    page.wait_for_function('__sceneCheck.state.sceneRefs.length===2')
                    restored_entry=page.evaluate('id=>__sceneCheck.state.sceneRefs.find(entry=>entry.id===id)',first_id)
                    assert restored_entry==entry
                    expect(page.locator('#feedback-note')).to_have_value(re.compile('.*🏞️1.*',re.S))
                    expect(page.locator('#feedback-note')).to_have_value(re.compile('.*🏞️2.*',re.S))
                    text=page.locator('#feedback-note')
                    positions=page.evaluate('__sceneCheck.promptReferenceText.ranges(document.getElementById("feedback-note").value).find(range=>range.entry.alias==="🏞️2")')
                    text.focus()
                    text.evaluate('(el,position)=>el.setSelectionRange(position,position)',positions['end'])
                    text.press('Backspace')
                    assert '🏞️2' not in text.input_value() and page.evaluate('__sceneCheck.state.sceneRefs.length')==2
                    text.press('Control+z');expect(text).to_have_value(re.compile('.*🏞️2.*',re.S))
                    assert page.evaluate('id=>__sceneCheck.state.sceneRefs.find(entry=>entry.id===id).preview_data_url',first_id)==entry['preview_data_url']
                    assert source_files(root/'examples')==source_before
                    print('PASS source update/unload cannot change frozen reference, IndexedDB restores exact preview/name/version, native delete and undo retain immutable binding',flush=True)

                    # Only aliases still present in the prompt become attachments.
                    page.locator('.prompt-image-chip[data-scene-ref-id="'+second_id+'"] .prompt-reference-remove').click()
                    assert '🏞️2' not in text.input_value()
                    assert page.evaluate('__sceneCheck.state.sceneRefs.length')==2
                    with page.expect_response(lambda response:response.request.method=='POST' and '/api/sessions/' in response.url and response.url.endswith('/feedback')) as submitted:
                        page.locator('#submit-button').click()
                    assert submitted.value.status==201,submitted.value.text()
                    feedback=submitted.value.json();refs=feedback['scene_refs']
                    assert len(refs)==1 and refs[0]['id']==first_id and refs[0]['source_scene_revision']==source_revision and refs[0]['read_only'] is True
                    assert (store.media_dir/Path(refs[0]['preview_url']).name).is_file() and Path(refs[0]['source_reference_image']['path']).is_file()
                    assert all(Path(item['path']).is_file() for item in refs[0]['assets'])
                    assert Path(refs[0]['snapshot_json_path']).is_file()
                    assert refs[0]['preview_camera']==entry['preview_camera'] and refs[0]['time_sec']==entry['time_sec']
                    plan=model_input_plan(feedback,store.data_dir)
                    summaries=plan['manifest']['scene_refs'];assert len(summaries)==1 and summaries[0]['token']=='[[scene:'+first_id+']]'
                    assert summaries[0]['source_scene_revision']==source_revision and summaries[0]['read_only'] is True and summaries[0]['glb_paths']
                    roles={role for image in plan['images'] for role in [image['role'],*(alias['role'] for alias in image.get('aliases',[]))]}
                    assert '相似场景预览（只读）' in roles and '相似场景参考原图（只读）' in roles
                    result=_visual_tool_result({'items':[feedback]},store.data_dir)
                    assert any(item.type=='image' for item in result.content)
                    assert any('read-only examples' in item.text for item in result.content if item.type=='text')
                    assert any(first_id in item.text and 'glb_paths' in item.text for item in result.content if item.type=='text')
                    page.wait_for_function('__sceneCheck.state.sceneRefs.length===0 && !__sceneCheck.state.submitting')
                    expect(text).to_have_value('')
                    assert page.locator('.prompt-image-chip[data-scene-ref-id]').count()==0
                    status,after=http(base,'/api/workspace/scene-references/'+first_id)
                    assert status==200 and after['scene']==first['scene']
                    assert store.scene()==initial_scene and source_files(root/'examples')==source_before
                    assert not errors,errors
                    targets.assert_not_called();models.assert_not_called()
                    assert all(url.endswith('/api/projects/import-folder') or url.endswith('/api/projects/folders') or url.endswith('/api/workspace/scene-references') or '/api/sessions/' in url and url.endswith('/feedback') for _,url in writes),writes
                    print('PASS actual feedback sends only the cited snapshot with preview/camera/GT/GLB paths and read-only MCP guidance; duplicate tokens dedup, fresh-round candidates clear while archived examples remain',flush=True)
                    browser.close()
            finally:
                server.shutdown();worker.join(5);server.server_close()
    print('ALL SCENE REFERENCE BROWSER CHECKS PASSED',flush=True)


if __name__=='__main__':main()
