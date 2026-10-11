#!/usr/bin/env python3
"""Connect imported scenes to fake Desktop tasks through real browser endpoints."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests'),str(ROOT/'backend/tests')]

from server import make_server
from test_folder_import_browser import http, model_catalog, png, source_files
from test_object_double_click_browser import model
from test_scene_unload_browser import draft, ready, sidebar, import_folder, row
from test_target_switch import TargetAdapter

OLD='01a0d906-146e-7762-a1f9-49baeda8e270'
NEW='01a0de73-9763-7432-8ca4-5892c0904234'


class Desktop:
    def __init__(self, directory):
        self.socket=Path('/tmp/fixture-desktop.sock')
        self.tasks={OLD:{'id':OLD,'name':'现有 6sol ultra','cwd':str(directory),
            'model':'gpt-6-sol','reasoningEffort':'ultra','status':{'type':'idle'},
            'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'}}}
        self.created=[];self.model_reads=0;self.adapters=[]

    def discovered(self, **_kwargs):
        return [(self.socket,copy.deepcopy(task)) for task in self.tasks.values()]

    def bridge(self, seed=None, **_kwargs):
        return Bridge(self,seed)

    def adapter(self, thread_id, on_event=None, **kwargs):
        initial=kwargs.pop('initial_bridge',None)
        adapter=TargetAdapter(thread_id,on_event,**kwargs)
        adapter.initial_bridge=initial
        self.adapters.append(adapter)
        return adapter


class Bridge:
    def __init__(self, desktop, seed):
        self.desktop=desktop;self.thread_id=seed;self.socket_path=desktop.socket;self.closed=False

    def list_models(self):
        self.desktop.model_reads+=1
        return [{'model':'gpt-6-astra','displayName':'Astra 测试模型','inputModalities':['text','image'],
            'supportedReasoningEfforts':[{'reasoningEffort':'high'},{'reasoningEffort':'ultra'}],
            'defaultReasoningEffort':'high','isDefault':True},
            {'model':'text-only','inputModalities':['text']}]

    def create_thread(self, model, cwd, *, reasoning_effort=None, title=None, permission_mode='workspace_write', config=None):
        self.desktop.created.append({'model':model,'cwd':Path(cwd),'effort':reasoning_effort,
                                     'title':title,'permission_mode':permission_mode,'config':config})
        self.desktop.tasks[NEW]={'id':NEW,'name':title,'cwd':str(cwd),'model':model,
            'reasoningEffort':reasoning_effort,'status':{'type':'idle'},'approvalPolicy':'never',
            'sandboxPolicy':{'type':'dangerFullAccess'}}
        self.thread_id=NEW
        return NEW

    def read_thread(self, **_kwargs):
        return copy.deepcopy(self.desktop.tasks[self.thread_id])

    def read_loaded_thread(self, thread_id):
        return copy.deepcopy(self.desktop.tasks[thread_id])

    def close(self):
        self.closed=True


def open_tasks(page):
    sidebar(page)
    page.locator('#task-dialog-button').click()
    page.wait_for_function("!__taskCheck.state.loadingTargets && !__taskCheck.state.loadingModels && __taskCheck.state.models?.length>0")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    cached=Path('/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    parser.add_argument('--browser-executable',default=str(cached) if cached.is_file() else None)
    args=parser.parse_args()
    from playwright.sync_api import expect,sync_playwright

    with tempfile.TemporaryDirectory(prefix='imported-task-browser-') as temporary:
        root=Path(temporary);original=root/'original';original.mkdir()
        model(original/'original.glb');png(original/'original.png','gray')
        sources=model_catalog(root/'catalog');before_source=source_files(root/'catalog')
        desktop=Desktop(root);existing_settings=copy.deepcopy(desktop.tasks[OLD])
        errors=[];writes=[]
        with patch('gateway.SharedThreadBridge.discover_loaded_threads',side_effect=desktop.discovered), \
             patch('gateway.SharedThreadBridge.connect_to_desktop',side_effect=desktop.bridge), \
             patch('gateway.SharedDesktopAdapter',side_effect=desktop.adapter), \
             patch('shared_thread_adapter.SharedDesktopAdapter',side_effect=desktop.adapter):
            server=make_server(port=0,data_dir=root/'data',project_dir=original,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            store=server.scene_store;gateway=server.workspace_gateway;registry=server.project_registry
            session_id=gateway.ensure()['session_id']
            store.import_model(str(original/'original.glb'),object_id='original_model',name='原始模型')
            gateway.add_reference_paths([str(original/'original.png')])
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True,**({'executable_path':args.browser_executable} if args.browser_executable else {}),
                        args=['--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    hook='\nwindow.__unloadCheck={state,cameraData,renderer};window.__taskCheck={state,ui,refreshWorkspace,loadTargets,loadModels,updateSubmitLabel};'
                    context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('request',lambda call:writes.append((call.method,call.url)) if call.method not in {'GET','HEAD','OPTIONS'} else None)
                    page.goto(server.browser_url(session_id));ready(page)
                    root_draft='原场景草稿继续保留。';draft(page,root_draft)
                    root_scene=copy.deepcopy(store.scene());root_session=copy.deepcopy(store.get_session(session_id))
                    imported=import_folder(page,root/'catalog')
                    assert len(imported['projects'])==2 and not imported['errors']
                    first,second=imported['projects'];first_id=first['project_id'];second_id=second['project_id']
                    assert desktop.model_reads==0 and desktop.adapters==[] and desktop.created==[]
                    first_context=registry.get(first_id);second_context=registry.get(second_id)
                    first_session=copy.deepcopy(first_context.store.get_session(first_context.gateway.ensure()['session_id']))
                    old_payload={'idempotency_key':'event-before-connect','scene_revision':first_context.store.scene()['revision'],'note':'旧插件反馈'}
                    old_event=first_context.gateway.submit(first_session['session_id'],old_payload)
                    old_id=old_event['feedback_id'];old_packet=copy.deepcopy(first_context.store.state['feedback'][0])
                    first_session=copy.deepcopy(first_context.store.get_session(first_session['session_id']))
                    second_session=copy.deepcopy(second_context.store.get_session(second_context.gateway.ensure()['session_id']))
                    first_scene=copy.deepcopy(first_context.store.scene());second_scene=copy.deepcopy(second_context.store.scene())
                    first_assets=source_files(first_context.store.assets_dir);second_assets=source_files(second_context.store.assets_dir)
                    assert first_context.gateway.adapter is None and second_context.gateway.adapter is None

                    row(page,first_id).locator('.project-item').click()
                    page.wait_for_url(re.compile(re.escape(base+'/p/'+first_id)+r'/.*'));ready(page)
                    first_draft='给现有 6sol 的场景草稿。';draft(page,first_draft)
                    assert desktop.model_reads==0 and desktop.adapters==[]
                    open_tasks(page)
                    expect(page.locator('#target-picker')).to_be_visible()
                    expect(page.locator('#sidebar-new-project')).to_be_hidden()
                    expect(page.locator('#current-target')).to_contain_text('尚未连接')
                    assert page.evaluate('__taskCheck.state.feedbackTransport')=='mcp_events'
                    assert first_context.gateway.adapter is None and desktop.adapters==[]
                    expect(page.locator('#target-select')).to_have_value(OLD)
                    root_capability=store.browser_token
                    status,_=http(base,f'/p/{first_id}/api/workspace/target',payload={'thread_id':OLD},capability=root_capability)
                    assert status==403
                    print('PASS imported event scenes keep zero adapters/model reads until opening Tasks; task picker appears by capability and scope credentials stay isolated',flush=True)

                    # The task becomes busy after its list entry was read.
                    desktop.tasks[OLD]['status']={'type':'active'}
                    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/api/workspace/target')) as rejected:
                        page.locator('#switch-target').click()
                    assert rejected.value.status==409
                    expect(page.locator('#toast')).to_contain_text('切换任务失败')
                    assert first_context.gateway.adapter is None
                    assert page.evaluate('__taskCheck.state.feedbackTransport')=='mcp_events'
                    expect(page.locator('#feedback-note')).to_have_value(first_draft)
                    desktop.tasks[OLD]['status']={'type':'idle'}
                    page.locator('#refresh-targets').click()
                    expect(page.locator('#switch-target')).to_be_enabled()
                    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/api/workspace/target')) as connected:
                        page.locator('#switch-target').click()
                    assert connected.value.status==200
                    page.wait_for_function('__taskCheck.state.boundThreadId==='+json.dumps(OLD))
                    assert page.evaluate('__taskCheck.state.feedbackTransport') is None
                    assert first_context.store.workspace()['thread_id']==OLD and first_context.gateway.feedback_transport=='legacy'
                    assert first_context.gateway.adapter.allow_bound_resume and first_context.gateway.adapter.permission_mode is None
                    expect(page.locator('#sidebar-new-project')).to_be_hidden()
                    assert desktop.tasks[OLD]==existing_settings and desktop.created==[]
                    assert first_context.store.scene()==first_scene and first_context.store.get_session(first_session['session_id'])==first_session
                    assert source_files(first_context.store.assets_dir)==first_assets
                    expect(page.locator('#feedback-note')).to_have_value(first_draft)
                    expect(page.locator('#queue-list .queue-event')).to_have_count(1)
                    expect(page.locator('#queue-list .queue-event')).to_contain_text('插件事件')
                    expect(page.locator('#queue-list .queue-event')).to_contain_text('等待插件订阅')
                    expect(page.locator('#queue-list .queue-event')).to_contain_text('不会改发到当前 Codex 任务')
                    assert page.locator('#queue-list .queue-event button').count()==0
                    assert first_context.store.workspace()['queue'][0].get('target_thread_id') is None
                    assert first_context.store.workspace()['queue'][0]['feedback_transport']=='mcp_events'
                    assert not first_context.gateway.adapter.sent
                    print('PASS busy binding reports 409 without changing transport/draft; explicit retry connects the existing task, retains its permission settings and scene evidence',flush=True)

                    page.locator('#close-projects').click()
                    page.wait_for_function("!document.getElementById('projects-dialog').open")
                    page.evaluate("payload=>{__taskCheck.state.pendingSubmission={key:payload.idempotency_key,payload,draftNote:'旧插件原草稿'};__taskCheck.updateSubmitLabel();}",old_payload)
                    with page.expect_response(lambda response:response.request.method=='POST' and '/api/sessions/' in response.url and response.url.endswith('/feedback')) as retried:
                        page.locator('#submit-button').click()
                    assert retried.value.status in (200,201) and retried.value.json()['delivery']['status'].startswith('event_'),(retried.value.status,retried.value.text())
                    expect(page.locator('#toast')).to_contain_text('插件')
                    expect(page.locator('#feedback-note')).to_have_value(first_draft)
                    assert first_context.store.state['feedback']==[old_packet]
                    assert first_context.store.workspace()['queue'][0]['feedback_id']==old_id
                    assert first_context.store.workspace()['queue'][0].get('target_thread_id') is None
                    assert not first_context.gateway.adapter.sent
                    print('PASS prior event row stays on its original route; idempotent retry after binding reports the actual plugin delivery and never sends into the direct task',flush=True)

                    sidebar(page)
                    page.locator('#tasks-dialog [data-sidebar-home]').click()
                    row(page,second_id).locator('.project-item').click()
                    page.wait_for_url(re.compile(re.escape(base+'/p/'+second_id)+r'/.*'));ready(page)
                    second_draft='交给新 Astra 的草稿。';draft(page,second_draft)
                    assert second_context.gateway.adapter is None and second_context.gateway.feedback_transport=='mcp_events'
                    open_tasks(page)
                    page.locator('#create-target-panel > summary').click()
                    page.locator('#create-title').fill('新场景 Astra')
                    page.locator('#create-effort').select_option('ultra')
                    page.locator('#create-permissions').select_option('full_access')
                    endpoint=re.compile(r'/api/workspace/targets$');held=[]
                    def hold(route):
                        if route.request.method!='POST':
                            route.continue_();return
                        response=route.fetch();assert response.status==201,response.text()
                        held.append((route,response));page.evaluate('window.__heldTaskCount='+str(len(held)))
                    page.route(endpoint,hold);page.evaluate('window.__heldTaskCount=0')
                    page.locator('#create-target').click()
                    page.wait_for_function('window.__heldTaskCount===1')
                    expect(page.locator('#create-target')).to_be_disabled()
                    expect(page.locator('#submit-button')).to_be_disabled()
                    page.locator('#create-target').evaluate("el=>{el.dispatchEvent(new MouseEvent('click',{bubbles:true}));el.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
                    assert len(held)==1 and len(desktop.created)==1
                    held[0][0].fulfill(response=held[0][1]);page.unroute(endpoint,hold)
                    page.wait_for_function('__taskCheck.state.boundThreadId==='+json.dumps(NEW)+' && !__taskCheck.state.creatingTarget')
                    assert second_context.gateway.feedback_transport=='legacy' and second_context.store.workspace()['created_thread_ids']==[NEW]
                    spec=desktop.created[0]
                    assert (spec['model'],spec['effort'],spec['title'],spec['permission_mode'],spec['cwd'])==('gpt-6-astra','ultra','新场景 Astra','full_access',sources[1])
                    env=spec['config']['mcp_servers']['scene_feedback']['env']
                    assert env['SCENE_FEEDBACK_DATA_DIR']==str(second_context.store.data_dir)
                    assert env['SCENE_FEEDBACK_PROJECT_DIR']==str(sources[1])
                    assert second_context.gateway.adapter.allow_owned_resume and second_context.gateway.adapter.permission_mode=='full_access'
                    assert second_context.store.scene()==second_scene and second_context.store.get_session(second_session['session_id'])==second_session
                    assert source_files(second_context.store.assets_dir)==second_assets
                    assert first_context.store.workspace()['thread_id']==OLD
                    expect(page.locator('#feedback-note')).to_have_value(second_draft)
                    assert source_files(root/'catalog')==before_source and store.scene()==root_scene and store.get_session(session_id)==root_session
                    print('PASS New Task creates exactly one owned Astra task with selected permissions/effort and this scene MCP paths; other scenes, original sources and both drafts remain intact',flush=True)

                    # Restart the isolated service on the same origin so its
                    # saved bindings, permissions and browser draft are restored.
                    port=server.server_port;current_url=page.url
                    server.shutdown();worker.join(5);server.server_close()
                    server=make_server(port=port,data_dir=root/'data',project_dir=original,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
                    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                    registry=server.project_registry;store=server.scene_store
                    page.goto(current_url);ready(page)
                    assert page.evaluate('__taskCheck.state.boundThreadId')==NEW
                    expect(page.locator('#feedback-note')).to_have_value(second_draft)
                    assert len(desktop.created)==1
                    restored_first=registry.get(first_id);restored_second=registry.get(second_id)
                    assert restored_first.gateway.feedback_transport=='legacy' and restored_second.gateway.feedback_transport=='legacy'
                    assert restored_first.gateway.adapter.thread_id==OLD and restored_first.gateway.adapter.permission_mode is None
                    assert restored_second.gateway.adapter.thread_id==NEW and restored_second.gateway.adapter.permission_mode=='full_access'
                    assert restored_first.store.scene()==first_scene and restored_second.store.scene()==second_scene
                    assert restored_first.store.get_session(first_session['session_id'])==first_session
                    assert restored_second.store.get_session(second_session['session_id'])==second_session
                    assert source_files(root/'catalog')==before_source
                    assert not errors and all(not adapter.sent for adapter in desktop.adapters),(errors,[adapter.sent for adapter in desktop.adapters])
                    assert all(url.endswith('/api/projects/folders') or url.endswith('/api/projects/import-folder') or url.endswith('/api/workspace/target') or url.endswith('/api/workspace/targets') or '/api/sessions/' in url and url.endswith('/feedback') for _,url in writes),writes
                    assert restored_first.store.state['feedback']==[old_packet]
                    assert all(context.store.state['feedback']==[] for context in registry.contexts() if context.project_id!=first_id)
                    print('PASS restart restores independent task bindings/permissions and the draft without creating another task or sending feedback; no browser errors or production model calls',flush=True)
                    browser.close()
            finally:
                server.shutdown();worker.join(5);server.server_close()
    print('ALL IMPORTED TASK CONNECTION BROWSER CHECKS PASSED',flush=True)


if __name__=='__main__':main()
