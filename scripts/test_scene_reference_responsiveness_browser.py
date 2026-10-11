#!/usr/bin/env python3
"""Keep native workbench input responsive while a dense scene preview renders."""
from __future__ import annotations

import argparse
from array import array
import base64
import copy
from io import BytesIO
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import uuid

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts'),str(ROOT/'tests')]
from gateway import WorkspaceGateway
from server import make_server
from test_object_double_click_browser import model
from test_folder_import_browser import http,png,source_files
from test_scene_unload_browser import ready,draft,sidebar,row


def dense_model(path: Path,divisions=900):
    """A curved mesh with 811,801 vertices and 1,620,000 visible triangles."""
    positions=array('f');normals=array('f');indices=array('I')
    for y in range(divisions+1):
        for x in range(divisions+1):
            positions.extend((x/divisions*2-1,y/divisions*2-1,
                .12*math.sin(x/divisions*math.pi*4)*math.cos(y/divisions*math.pi*4)))
            normals.extend((0,0,1))
    for y in range(divisions):
        for x in range(divisions):
            a=y*(divisions+1)+x;b=a+divisions+1
            indices.extend((a,a+1,b,b,a+1,b+1))
    binary=positions.tobytes()+normals.tobytes()+indices.tobytes()
    position_bytes=len(positions)*4;normal_bytes=len(normals)*4
    document={
        'asset':{'version':'2.0'},'scene':0,'scenes':[{'nodes':[0]}],'nodes':[{'mesh':0}],
        'meshes':[{'primitives':[{'attributes':{'POSITION':0,'NORMAL':1},'indices':2,'material':0}]}],
        'materials':[{'doubleSided':True,'pbrMetallicRoughness':{
            'baseColorFactor':[.08,.2,.38,1],'metallicFactor':0,'roughnessFactor':.8}}],
        'buffers':[{'byteLength':len(binary)}],
        'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':position_bytes},
            {'buffer':0,'byteOffset':position_bytes,'byteLength':normal_bytes},
            {'buffer':0,'byteOffset':position_bytes+normal_bytes,'byteLength':len(indices)*4}],
        'accessors':[{'bufferView':0,'componentType':5126,'count':len(positions)//3,
            'type':'VEC3','min':[-1,-1,-.12],'max':[1,1,.12]},
            {'bufferView':1,'componentType':5126,'count':len(normals)//3,'type':'VEC3'},
            {'bufferView':2,'componentType':5125,'count':len(indices),'type':'SCALAR'}],
    }
    encoded=json.dumps(document).encode();encoded+=b' '*(-len(encoded)%4)
    path.write_bytes(struct.pack('<4sII',b'glTF',2,28+len(encoded)+len(binary))+
        struct.pack('<I4s',len(encoded),b'JSON')+encoded+
        struct.pack('<I4s',len(binary),b'BIN\0')+binary)


TELEMETRY=r"""
window.__responsive={jobs:0,completed:0,workers:0,pending:0,longtasks:[],writes:[]};
const observer=new PerformanceObserver(items=>__responsive.longtasks.push(...items.getEntries().map(
  item=>({start:item.startTime,duration:item.duration}))));
observer.observe({type:'longtask',buffered:true});
window.__resetLongTasks=()=>{observer.takeRecords();__responsive.longtasks=[];};
const NativeWorker=window.Worker;
window.Worker=class extends NativeWorker {
  constructor(...args){super(...args);__responsive.workers++;
    this.addEventListener('message',()=>{__responsive.completed++;__responsive.pending--;});}
  postMessage(...args){__responsive.jobs++;__responsive.pending++;return super.postMessage(...args);}
};
const put=IDBObjectStore.prototype.put;
IDBObjectStore.prototype.put=function(value,key){
  if(this.name==='drafts' && typeof key==='string')__responsive.writes.push(key);
  return put.call(this,value,key);
};
"""


def capture(page,project_id):
    sidebar(page)
    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/api/workspace/scene-references')) as response:
        row(page,project_id).locator('.project-cite-trigger').click()
    assert response.value.status==201,response.value.text()
    record=response.value.json()
    page.wait_for_function('__referenceSpeed.state.citingSceneProject===null && __referenceSpeed.state.sceneRefs.length===1')
    return record


def idle(page):
    page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable',default='/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome')
    args=parser.parse_args()
    from playwright.sync_api import expect,sync_playwright
    with tempfile.TemporaryDirectory(prefix='scene-reference-responsive-') as temporary:
        root=Path(temporary);project=root/'current';source=root/'dense';project.mkdir();source.mkdir()
        model(project/'current.glb');png(project/'gt.png','gray')
        dense_model(source/'dense.glb');png(source/'gt.png','navy')
        (source/'workbench-ready.json').write_text(json.dumps({'status':'ready','name':'Dense reference',
            'scene_glb':'dense.glb','reference_images':['gt.png']}))
        original_files=source_files(source)
        errors=[];metrics={};assets=[]
        with patch.object(WorkspaceGateway,'create_target',side_effect=AssertionError('preview created a task')) as tasks, \
             patch.object(WorkspaceGateway,'list_models',side_effect=AssertionError('preview queried a model')) as models:
            server=make_server(port=0,data_dir=root/'data',project_dir=project,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            store=server.scene_store;session=server.workspace_gateway.ensure()['session_id']
            store.import_model(str(project/'current.glb'),object_id='current',name='Current')
            server.workspace_gateway.add_reference_paths([str(project/'gt.png')])
            original_scene=copy.deepcopy(store.scene())
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                status,result=http(base,'/api/projects/import-folder',payload={'path':str(source),'request_id':uuid.uuid4().hex},capability=store.browser_token)
                assert status==200 and result['projects'],result
                source_id=result['projects'][0]['project_id']
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True,executable_path=args.browser_executable,args=[
                        '--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    context.add_init_script(TELEMETRY)
                    hook='\nwindow.__unloadCheck={state,cameraData,renderer};window.__referenceSpeed={state,cameraData,sceneReferenceStore};'
                    context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
                    context.on('request',lambda call:assets.append(call.url) if call.url.endswith('.glb') else None)
                    page.goto(server.browser_url(session));ready(page);draft(page,'保留草稿')
                    before=page.evaluate('JSON.stringify({camera:__referenceSpeed.cameraData(),objects:__referenceSpeed.state.sceneObjects,task:__referenceSpeed.state.boundThreadId})')

                    # A real Escape key and textarea input run while the dense
                    # model is still being decoded and rendered in the worker.
                    sidebar(page);page.evaluate('__resetLongTasks()')
                    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/api/workspace/scene-references')) as requested:
                        row(page,source_id).locator('.project-cite-trigger').click()
                    assert requested.value.status==201,requested.value.text()
                    record=requested.value.json()
                    page.wait_for_function('__responsive.pending>0 && __referenceSpeed.state.citingSceneProject')
                    start=time.perf_counter();page.keyboard.press('Escape');key_elapsed=time.perf_counter()-start
                    assert key_elapsed<.3,('Escape blocked while worker rendered',key_elapsed)
                    page.wait_for_function('!document.getElementById("projects-dialog").open')
                    field=page.locator('#feedback-note');start=time.perf_counter();field.fill('继续编辑');input_elapsed=time.perf_counter()-start
                    assert input_elapsed<.3,('textarea blocked while worker rendered',input_elapsed)
                    page.wait_for_function('__referenceSpeed.state.citingSceneProject===null && __responsive.completed===1')
                    idle(page);metrics['cold_capture']=page.evaluate('structuredClone(__responsive)')
                    metrics['escape_ms']=round(key_elapsed*1000,1);metrics['input_ms']=round(input_elapsed*1000,1)
                    Path('/tmp/scene_reference_responsiveness_metrics.json').write_text(json.dumps(metrics,indent=2))
                    assert max([item['duration'] for item in metrics['cold_capture']['longtasks']] or [0])<250,metrics
                    expect(field).to_have_value('继续编辑')
                    assert page.evaluate('__referenceSpeed.state.sceneRefs.length')==0
                    print('PASS 39 MB / 1.62 million-triangle preview runs in a real worker; Escape and native prompt input stay responsive, changed draft cancels only the pending insertion',flush=True)

                    # The completed immutable preview is cached even when its
                    # original insertion became stale. Canonical server capture
                    # still occurs and confirms which image belongs to the ID.
                    cached=page.evaluate('async id=>__referenceSpeed.sceneReferenceStore.loadPreview(__referenceSpeed.state.sessionId,id)',record['id'])
                    assert set(cached)=={'id','preview_data_url','preview_camera','width','height','time_sec'}
                    assert assets.count(base+record['assets'][0]['url'])==1,assets
                    jobs=page.evaluate('__responsive.jobs');calls=len(assets)
                    captured=capture(page,source_id);assert captured['id']==record['id']
                    entry=page.evaluate('__referenceSpeed.state.sceneRefs[0]')
                    assert entry['preview_data_url']==cached['preview_data_url']
                    image=Image.open(BytesIO(base64.b64decode(entry['preview_data_url'].split(',',1)[1]))).convert('RGB')
                    assert image.size==(640,400)
                    pixels=getattr(image,'get_flattened_data',image.getdata)()
                    assert sum(max(abs(pixel[n]-(234,233,227)[n]) for n in range(3))>15 for pixel in pixels)>1000
                    assert page.evaluate('__responsive.jobs')==jobs and len(assets)==calls
                    await_saved=page.evaluate('__referenceSpeed.sceneReferenceStore.pending.then(()=>true)');assert await_saved
                    writes=page.evaluate('__responsive.writes.length')
                    repeated=capture(page,source_id);assert repeated['id']==record['id']
                    assert page.evaluate('__responsive.jobs')==jobs and len(assets)==calls
                    assert page.evaluate('offset=>__responsive.writes.slice(offset).every(key=>!key.startsWith("prompt-scenes:") && !key.startsWith("scene-previews:"))',writes)
                    assert field.input_value().strip()=='继续编辑 🏞️1 🏞️1'
                    assert page.evaluate('JSON.stringify({camera:__referenceSpeed.cameraData(),objects:__referenceSpeed.state.sceneObjects,task:__referenceSpeed.state.boundThreadId})')==before
                    print('PASS cached canonical preview has real model pixels and no source metadata; same-round repeat makes no worker job, asset fetch or scene-reference IndexedDB rewrite',flush=True)

                    with page.expect_response(lambda response:response.request.method=='POST' and response.url.endswith('/feedback')) as submitted:
                        page.locator('#submit-button').click()
                    assert submitted.value.status==201,submitted.value.text()
                    page.wait_for_function('__referenceSpeed.state.sceneRefs.length===0 && !__referenceSpeed.state.submitting')
                    expect(field).to_have_value('')
                    next_round=capture(page,source_id);assert next_round['id']==record['id']
                    assert page.evaluate('__responsive.jobs')==jobs and len(assets)==calls
                    assert page.evaluate('__referenceSpeed.state.sceneRefs[0].preview_data_url')==cached['preview_data_url']
                    page.evaluate('__referenceSpeed.sceneReferenceStore.pending.then(()=>true)')
                    page.reload();ready(page);page.wait_for_function('__referenceSpeed.state.sceneRefs.length===1')
                    assert page.evaluate('__responsive.jobs')==0
                    assert page.evaluate('__referenceSpeed.state.sceneRefs[0].preview_data_url')==cached['preview_data_url']
                    assert page.locator('#feedback-note').input_value().strip()=='🏞️1'
                    assert store.scene()==original_scene and source_files(source)==original_files
                    assert not errors,errors
                    tasks.assert_not_called();models.assert_not_called()
                    metrics['file_bytes']=(source/'dense.glb').stat().st_size
                    Path('/tmp/scene_reference_responsiveness_metrics.json').write_text(json.dumps(metrics,indent=2))
                    print('PASS successful feedback clears candidates while immutable preview cache survives the new round and reload; current scene/source files/task remain unchanged',flush=True)
                    print(json.dumps({'file_bytes':metrics['file_bytes'],'escape_ms':metrics['escape_ms'],
                        'input_ms':metrics['input_ms'],'longtask_ms':[item['duration'] for item in metrics['cold_capture']['longtasks']]}),flush=True)
                    browser.close()
            finally:server.shutdown();thread.join(5);server.server_close()
    print('ALL SCENE REFERENCE RESPONSIVENESS CHECKS PASSED',flush=True)


if __name__=='__main__':main()
