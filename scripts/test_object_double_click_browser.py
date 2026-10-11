#!/usr/bin/env python3
"""Exercise real selection toggles and double clicks without a live Codex task."""
import argparse
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts')]
from server import make_server
from test_prompt_drag_browser import image_data


def model(path):
    data=struct.pack('<9f3H',-.6,-.5,0,.6,-.5,0,0,.7,0,0,1,2)+b'\0\0'
    doc={'asset':{'version':'2.0'},'scene':0,'scenes':[{'nodes':[0]}],
         'nodes':[{'name':'Cabinet','children':[1]},{'name':'Door','mesh':0}],
         'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1,'material':0}]}],
         'materials':[{'doubleSided':True}], 'buffers':[{'byteLength':len(data)}],
         'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':36,'target':34962},
                        {'buffer':0,'byteOffset':36,'byteLength':6,'target':34963}],
         'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3','min':[-.6,-.5,0],'max':[.6,.7,0]},
                      {'bufferView':1,'componentType':5123,'count':3,'type':'SCALAR'}]}
    encoded=json.dumps(doc).encode();encoded+=b' '*(-len(encoded)%4)
    path.write_bytes(struct.pack('<4sII',b'glTF',2,28+len(encoded)+len(data))+
                     struct.pack('<I4s',len(encoded),b'JSON')+encoded+struct.pack('<I4s',len(data),b'BIN\0')+data)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--browser-executable');args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='object-dblclick-') as folder:
        root=Path(folder);project=root/'project';project.mkdir()
        server=make_server(port=0,data_dir=root/'data',project_dir=project,web_dir=ROOT/'web',external_review=True,feedback_transport='mcp_events')
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        store=server.scene_store;session=server.workspace_gateway.ensure()['session_id']
        store.replace_scene(1,[{'id':'box','name':'独立盒子','type':'box','position':[1.3,0,0],'size':[.3,.3,.3]}])
        asset=root/'model.glb';model(asset);store.import_model(str(asset),object_id='fixture_model',name='柜子模型')
        camera={'camera_to_world':[[1,0,0,0],[0,1,0,0],[0,0,1,3],[0,0,0,1]],'intrinsics':{'width':320,'height':240,'fx':300,'fy':300,'cx':160,'cy':120}}
        store.set_reference_clip(session,{'name':'参考','fps':1,'frames':[{'name':'参考.png','data_url':image_data('navy'),'time_sec':0,'camera':camera}]})
        errors=[];writes=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({'executable_path':args.browser_executable} if args.browser_executable else {}),args=['--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                context=browser.new_context(viewport={'width':1440,'height':1000})
                hook='\nwindow.__dblRefs={state,THREE,camera,cameraData,renderer,setMode,promptText};'
                context.route('**/app.js',lambda route:route.fulfill(status=200,content_type='application/javascript',body=(ROOT/'web/app.js').read_text()+hook))
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:writes.append(r.method) if r.method not in ['GET','HEAD','OPTIONS'] else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                page.wait_for_function("window.__dblRefs && __dblRefs.state.workspaceReady && !__dblRefs.state.sceneLoading && !__dblRefs.state.seeking && document.getElementById('reference-image').naturalWidth>0")
                canvas=page.locator('#viewport canvas');note=page.locator('#feedback-note')
                def point():
                    return page.evaluate("""()=>{const m=__dblRefs,n=m.state.objectNodes.get('fixture_model').userData.gltfRoot.children[0].children[0];n.updateWorldMatrix(true,false);const p=new m.THREE.Vector3(0,-.1,0).applyMatrix4(n.matrixWorld).project(m.camera),r=m.renderer.domElement.getBoundingClientRect();return {x:r.x+(p.x+1)*r.width/2,y:r.y+(1-p.y)*r.height/2};}""")
                def evidence():return page.evaluate('JSON.stringify({camera:__dblRefs.cameraData(),objects:__dblRefs.state.sceneObjects,snapshot:__dblRefs.state.snapshot,marks:__dblRefs.state.annotations})')
                page.evaluate("__dblRefs.setMode('point','reference')")
                note.fill('开始  后面');note.evaluate('el=>el.setSelectionRange(3,3)');before=evidence();p=point()
                page.mouse.click(p['x'],p['y']);assert note.input_value()=='开始  后面'
                assert page.evaluate("__dblRefs.state.selectedId==='fixture_model'")
                page.wait_for_timeout(600)
                page.mouse.click(p['x'],p['y'])
                page.wait_for_function("__dblRefs.state.selectedId===null")
                assert page.evaluate('__dblRefs.state.selectedSceneNode===null && __dblRefs.state.lastPickedDetailNode===null')
                assert before==evidence() and note.input_value()=='开始  后面'
                page.mouse.click(p['x'],p['y'])
                page.wait_for_timeout(600)
                print('PASS repeated viewport single-click clears object, node and detail selection without changing evidence',flush=True)
                page.mouse.dblclick(p['x'],p['y'],delay=70)
                page.wait_for_timeout(600)
                assert page.evaluate("__dblRefs.state.selectedId==='fixture_model'")
                assert page.evaluate("__dblRefs.promptText().split('[[node:fixture_model:0]]').length-1")==1
                assert '🧊1' in note.input_value() and '🧩' not in note.input_value()
                assert note.input_value().startswith('开始') and note.input_value().endswith(' 后面')
                assert before==evidence() and page.evaluate("__dblRefs.state.paneModes.reference==='point'")
                print('PASS single-click selection; real item double-click cites its precise node without changing evidence or left tool',flush=True)

                page.locator('[data-selection-level=part]').click();p=point();before=evidence();page.mouse.dblclick(p['x'],p['y'],delay=70)
                assert '[[node:fixture_model:0/0]]' in page.evaluate('__dblRefs.promptText()') and before==evidence()
                assert '🧩1' in note.input_value()
                assert len(page.evaluate('__dblRefs.state.referencedSceneNodes'))==2
                page.wait_for_timeout(600)
                assert page.evaluate('JSON.stringify(__dblRefs.state.selectedSceneNode.node_path)')=='[0,0]'
                page.mouse.click(p['x'],p['y'])
                page.wait_for_function('__dblRefs.state.selectedId===null')
                page.mouse.click(p['x'],p['y'])
                page.wait_for_timeout(600)
                print('PASS part double-click keeps the hit child selected; repeated part single-click deselects it',flush=True)

                page.evaluate("document.getElementById('references-dialog').showModal()")
                page.locator('.selected-name').dblclick()
                assert page.evaluate("__dblRefs.promptText().split('[[node:fixture_model:0/0]]').length-1")==2
                page.evaluate("document.getElementById('references-dialog').showModal()")
                row=page.locator('.object-item[data-object-id=box]');element=row.element_handle();before=evidence()
                row.click();assert element.evaluate('el=>el.isConnected')
                page.wait_for_timeout(600)
                row.click();page.wait_for_function('__dblRefs.state.selectedId===null')
                row.click();page.wait_for_timeout(600)
                row.click()
                page.locator('.object-item[data-object-id=fixture_model]').click()
                page.wait_for_timeout(600)
                assert page.evaluate("__dblRefs.state.selectedId==='fixture_model'")
                row.click();page.wait_for_timeout(600)
                row.dblclick();assert '[[object:box]]' in page.evaluate('__dblRefs.promptText()') and before==evidence()
                page.wait_for_timeout(600)
                assert page.evaluate("__dblRefs.state.selectedId==='box'")
                assert '🧊2' in note.input_value()
                print('PASS selected-name and object-list double-click; first selection preserves the clicked row',flush=True)

                note.fill('');page.evaluate("document.getElementById('references-dialog').showModal()")
                start=page.locator('.object-item[data-object-id=box]').bounding_box();end=note.bounding_box()
                page.mouse.move(start['x']+start['width']/2,start['y']+start['height']/2);page.mouse.down()
                page.mouse.move(end['x']+40,end['y']+end['height']/2,steps=18);page.mouse.up()
                page.wait_for_function("__dblRefs.promptText().includes('[[object:box]]')")
                assert note.input_value()=='🧊2'
                print('PASS existing native object drag still inserts the same exact reference',flush=True)

                note.fill('');p=point();page.mouse.move(p['x'],p['y']);page.mouse.down();page.mouse.move(p['x']+45,p['y']+20,steps=4);page.mouse.move(p['x'],p['y'],steps=4);page.mouse.up()
                page.mouse.click(p['x'],p['y'])
                canvas.dispatch_event('dblclick',{'button':0,'clientX':p['x'],'clientY':p['y']})
                assert note.input_value()==''
                canvas.dispatch_event('pointercancel',{'pointerId':1})
                canvas.dispatch_event('dblclick',{'button':0,'clientX':p['x'],'clientY':p['y']})
                assert note.input_value()==''
                before=page.evaluate('__dblRefs.cameraData()');canvas.focus();page.keyboard.press('f');page.wait_for_timeout(800)
                assert page.evaluate('__dblRefs.cameraData()')!=before
                print('PASS drag-return/cancel cannot quote; F still focuses selection',flush=True)
                assert not errors and not writes and not store.state['feedback'],(errors,writes)
                browser.close()
        finally:server.shutdown();worker.join(5);server.server_close()
    print('ALL OBJECT DOUBLE-CLICK BROWSER CHECKS PASSED',flush=True)


if __name__=='__main__':main()
