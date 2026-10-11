#!/usr/bin/env python3
"""Compare actual rendered selection silhouettes, animation and evidence pixels."""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import threading

from PIL import Image, ImageChops, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts"), str(ROOT / "tests")]

from server import make_server
from test_prompt_drag_browser import image_data
from workspace_ui_helpers import control


def model(path: Path) -> None:
    """A real animated torus and a separate box share one selectable item."""
    binary = bytearray()
    views, accessors = [], []

    def attribute(values, width, *, indices=False, bounds=False):
        while len(binary) % 4:
            binary.append(0)
        start = len(binary)
        binary.extend(struct.pack("<" + ("H" if indices else "f") * len(values), *values))
        views.append({"buffer": 0, "byteOffset": start, "byteLength": len(binary) - start})
        accessor = {"bufferView": len(views)-1, "componentType": 5123 if indices else 5126,
                    "count": len(values)//width, "type": {1: "SCALAR", 2: "VEC2", 3: "VEC3"}[width]}
        if bounds:
            accessor.update(min=[min(values[index::width]) for index in range(width)],
                            max=[max(values[index::width]) for index in range(width)])
        accessors.append(accessor)
        return len(accessors)-1

    positions, normals, uvs, indices = [], [], [], []
    segments, sides = 48, 16
    for segment in range(segments+1):
        angle = segment*2*math.pi/segments
        for side in range(sides+1):
            cross = side*2*math.pi/sides
            radius = .72+.18*math.cos(cross)
            positions.extend([radius*math.cos(angle), radius*math.sin(angle), .18*math.sin(cross)])
            normals.extend([math.cos(cross)*math.cos(angle), math.cos(cross)*math.sin(angle), math.sin(cross)])
            uvs.extend([segment/segments, side/sides])
    for segment in range(segments):
        for side in range(sides):
            left = segment*(sides+1)+side
            right = left+sides+1
            indices.extend([left, right, left+1, right, right+1, left+1])
    ring = {"primitives": [{"attributes": {"POSITION": attribute(positions, 3, bounds=True),
              "NORMAL": attribute(normals, 3), "TEXCOORD_0": attribute(uvs, 2)},
              "indices": attribute(indices, 1, indices=True), "material": 0}]}
    positions, normals, indices = [], [], []
    for normal, corners in (
        ((0,0,1), [(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]),
        ((0,0,-1), [(1,-1,-1),(-1,-1,-1),(-1,1,-1),(1,1,-1)]),
        ((0,1,0), [(-1,1,1),(1,1,1),(1,1,-1),(-1,1,-1)]),
        ((0,-1,0), [(-1,-1,-1),(1,-1,-1),(1,-1,1),(-1,-1,1)]),
        ((1,0,0), [(1,-1,1),(1,-1,-1),(1,1,-1),(1,1,1)]),
        ((-1,0,0), [(-1,-1,-1),(-1,-1,1),(-1,1,1),(-1,1,-1)]),
    ):
        start = len(positions)//3
        for corner in corners:
            positions.extend([value*.2 for value in corner]);normals.extend(normal)
        indices.extend([start,start+1,start+2,start,start+2,start+3])
    box = {"primitives": [{"attributes": {"POSITION": attribute(positions, 3, bounds=True),
             "NORMAL": attribute(normals, 3)}, "indices": attribute(indices, 1, indices=True), "material": 0}]}
    times = attribute([0,1], 1, bounds=True)
    translation = attribute([0,0,0,.35,.15,0], 3)
    while len(binary) % 4:
        binary.append(0)
    document = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Assembly", "children": [1,2]}, {"name": "Ring", "mesh": 0},
                  {"name": "Handle", "mesh": 1, "translation": [1.3,.1,0]}],
        "meshes": [ring,box], "materials": [{"doubleSided": True, "pbrMetallicRoughness": {
            "baseColorFactor": [.08,.2,.38,1], "metallicFactor": 0, "roughnessFactor": .8}}],
        "animations": [{"name": "ring movement", "samplers": [{"input": times, "output": translation, "interpolation": "LINEAR"}],
                        "channels": [{"sampler": 0, "target": {"node": 1, "path": "translation"}}]}],
        "bufferViews": views, "accessors": accessors, "buffers": [{"byteLength": len(binary)}]}
    encoded = json.dumps(document).encode()
    encoded += b" " * (-len(encoded) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 28+len(encoded)+len(binary)) +
                    struct.pack("<I4s", len(encoded), b"JSON") + encoded +
                    struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def picture(value: str) -> Image.Image:
    return Image.open(BytesIO(base64.b64decode(value.split(",",1)[1]))).convert("RGB")


def pixels(image: Image.Image):
    return getattr(image,"get_flattened_data",image.getdata)()


def mask(image: Image.Image, *, red=False) -> Image.Image:
    result = Image.new("L", image.size)
    result.putdata([255 if (r>120 and r-g>45 and r-b>45 if red else b-r>25 and b-g>15) else 0
                    for r,g,b in pixels(image)])
    return result


def count(image: Image.Image) -> int:
    return image.histogram()[255]


def silhouette(selected: Image.Image, original: Image.Image, dpr=1) -> dict:
    assert selected.size == original.size
    red, geometry = mask(selected, red=True), mask(original)
    red_count = count(red)
    assert red_count > 120*dpr, ("missing red selection outline", red_count)
    radius = math.ceil(4*dpr)
    far = ImageChops.multiply(red, ImageChops.invert(geometry.filter(ImageFilter.MaxFilter(radius*2+1))))
    interior = ImageChops.multiply(red, geometry.filter(ImageFilter.MinFilter(radius*2+1)))
    assert count(far) <= max(8, red_count*.015), ("red pixels beyond actual silhouette", count(far), red_count)
    assert count(interior) <= max(8, red_count*.01), ("interior painted instead of contour", count(interior), red_count)
    points = [(index % red.width,index // red.width) for index,value in enumerate(pixels(red)) if value]
    return {"red": red, "count": red_count, "center": [sum(p[0] for p in points)/red_count, sum(p[1] for p in points)/red_count]}


def capture(page, *, original=False, jpeg=False, geometry=False) -> Image.Image:
    return picture(page.evaluate("""({original,jpeg,geometry})=>{const m=__outlineCheck,visible=m.feedbackLayer.visible,color=m.handle.material.color.clone();
        if(geometry && m.selectionOutline.root===m.ring)m.handle.material.color.set('#eae9e3');
        m.feedbackLayer.visible=!original;try {m.renderLiveScene();return jpeg?m.captureLiveScene():m.renderer.domElement.toDataURL('image/png');}
        finally {m.handle.material.color.copy(color);m.feedbackLayer.visible=visible;m.renderLiveScene();}}""", {"original": original, "jpeg": jpeg, "geometry": geometry}))


def point(page, node="ring", local=(0,.72,0)) -> dict:
    return page.evaluate("""({name,local})=>{const m=__outlineCheck,node=m[name];node.updateWorldMatrix(true,false);
        const p=new m.THREE.Vector3(...local).applyMatrix4(node.matrixWorld).project(m.camera),r=m.renderer.domElement.getBoundingClientRect();
        return {x:r.x+(p.x+1)*r.width/2,y:r.y+(1-p.y)*r.height/2,px:(p.x+1)*m.renderer.domElement.width/2,py:(1-p.y)*m.renderer.domElement.height/2};} """, {"name": node, "local": list(local)})


def in_region(red: Image.Image, center: dict, radius: float) -> int:
    return count(red.crop((max(0,int(center["px"]-radius)),max(0,int(center["py"]-radius)),
                           min(red.width,int(center["px"]+radius)),min(red.height,int(center["py"]+radius)))))


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    cached=Path("/tmp/dynamic-browser-cache/chromium-1243/chrome-linux64/chrome")
    parser.add_argument("--browser-executable", default=str(cached) if cached.is_file() else None)
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="selection-outline-browser-") as temporary:
        root=Path(temporary);project=root/"project";project.mkdir()
        asset=project/"torus.glb";model(asset);source=asset.read_bytes()
        server=make_server(port=0,data_dir=root/"data",project_dir=project,web_dir=ROOT/"web",external_review=True,feedback_transport="mcp_events")
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        store=server.scene_store;session=server.workspace_gateway.ensure()["session_id"]
        store.replace_scene(1,[])
        store.import_model(str(asset),object_id="assembly",name="有孔组合件",position=[0,1.1,0])
        store.set_reference_clip(session,{"name":"reference","fps":1,"frames":[{"name":"frame.png","data_url":image_data("navy"),"time_sec":0}]})
        scene_before=store.scene();errors=[];writes=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({"executable_path":args.browser_executable} if args.browser_executable else {}),
                    args=["--no-sandbox","--no-proxy-server","--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader"])
                context=browser.new_context(viewport={"width":1440,"height":1000})
                hook="\nwindow.__outlineCheck={state,THREE,camera,renderer,controls,threeScene,feedbackLayer,selectionOutline,renderLiveScene,selectObject,captureLiveScene,applyAnimationTime,renderSelection,resizeScene,loadScene};"
                context.route("**/app.js",lambda route:route.fulfill(status=200,content_type="application/javascript",body=(ROOT/"web/app.js").read_text()+hook))
                page=context.new_page();page.on("pageerror",lambda error:errors.append(str(error)))
                page.on("console",lambda message:errors.append(message.text) if message.type=="error" else None)
                page.on("request",lambda call:writes.append((call.method,call.url)) if call.method not in {"GET","HEAD","OPTIONS"} else None)
                page.goto(server.browser_url(session))
                page.wait_for_function("window.__outlineCheck && __outlineCheck.state.workspaceReady && !__outlineCheck.state.sceneLoading && [...__outlineCheck.state.objectNodes.values()].every(node=>node.userData.loaded)")
                page.evaluate("""()=>{const m=__outlineCheck,group=m.state.objectNodes.get('assembly').userData.gltfRoot.children[0];
                    m.ring=group.children[0];m.handle=group.children[1];m.group=group;
                    for(const mesh of [m.ring,m.handle]) mesh.material=new m.THREE.MeshBasicMaterial({color:'#385f86',toneMapped:false,side:m.THREE.DoubleSide});
                    m.controls.cancelTransition();m.controls.enabled=false;m.camera.position.set(.3,1.1,4.5);m.controls.target.set(.3,1.1,0);m.camera.up.set(0,1,0);m.camera.lookAt(m.controls.target);
                    m.applyAnimationTime(0);m.renderLiveScene();m.hierarchy=JSON.stringify(group.children.map(node=>[node.uuid,node.children.map(child=>child.uuid)]));} """)
                original=capture(page,original=True)
                p=point(page);page.mouse.click(p["x"],p["y"])
                page.wait_for_function("__outlineCheck.state.selectedSceneNode?.node_path.join('/')==='0'")
                item=silhouette(capture(page),original)
                handle_center=point(page,"handle",(0,0,0))
                assert in_region(item["red"],handle_center,60)>40
                hole=point(page,"ring",(0,0,0));inside=point(page,"ring",(.54,0,0));outside=point(page,"ring",(.9,0,0))
                inner_radius=abs(inside["px"]-hole["px"]);outer_radius=abs(outside["px"]-hole["px"])
                inner=sum(value>0 for index,value in enumerate(pixels(item["red"]))
                          if .85*inner_radius<math.hypot(index%item["red"].width-hole["px"],index//item["red"].width-hole["py"])<1.2*inner_radius)
                assert inner>100 and in_region(item["red"],hole,inner_radius*.45)==0
                for dx,dy in ((1,1),(1,-1),(-1,1),(-1,-1)):
                    corner={"px":hole["px"]+dx*outer_radius*.95,"py":hole["py"]+dy*outer_radius*.95}
                    assert in_region(item["red"],corner,5)==0
                capture(page).save("/tmp/scene_feedback_selection_outline.png")
                print("PASS native item click outlines both parts, including inner hole and outer torus silhouette, with no bounding-box corners or filled interior",flush=True)

                control(page,'[data-selection-level="part"]').click()
                # Changing level already selects the hit part. Pick a sibling first,
                # then return to the ring; a second click on the ring now deselects.
                p=point(page,"handle",(0,0,0));page.mouse.click(p["x"],p["y"])
                page.wait_for_function("__outlineCheck.state.selectedSceneNode?.node_path.join('/')==='0/1'")
                p=point(page);page.mouse.click(p["x"],p["y"])
                page.wait_for_function("__outlineCheck.state.selectedSceneNode?.node_path.join('/')==='0/0'")
                part=silhouette(capture(page),capture(page,original=True,geometry=True))
                assert in_region(part["red"],handle_center,60)==0
                assert page.evaluate("__outlineCheck.selectionOutline.root===__outlineCheck.ring")
                page.wait_for_timeout(600)
                page.mouse.click(p["x"],p["y"])
                page.wait_for_function("__outlineCheck.state.selectedId===null")
                assert count(mask(capture(page),red=True))==0
                assert page.evaluate("__outlineCheck.selectionOutline.root===null && __outlineCheck.selectionOutline.materials.size===0")
                p=point(page);page.mouse.click(p["x"],p["y"])
                page.evaluate("__outlineCheck.applyAnimationTime(.8)")
                moved=silhouette(capture(page),capture(page,original=True,geometry=True))
                center=point(page,"ring",(0,0,0))
                assert math.hypot(moved["center"][0]-center["px"],moved["center"][1]-center["py"])<6
                assert math.hypot(moved["center"][0]-part["center"][0],moved["center"][1]-part["center"][1])>20
                assert page.evaluate("__outlineCheck.selectionOutline.root===__outlineCheck.ring && __outlineCheck.hierarchy===JSON.stringify(__outlineCheck.group.children.map(node=>[node.uuid,node.children.map(child=>child.uuid)]))")
                page.evaluate("__outlineCheck.camera.position.set(2.7,2,4.2);__outlineCheck.camera.lookAt(__outlineCheck.controls.target);__outlineCheck.renderLiveScene()")
                silhouette(capture(page),capture(page,original=True,geometry=True))
                print("PASS native part selection excludes sibling, clear removes outline, animation and oblique camera follow actual mesh without changing node paths",flush=True)

                page.set_viewport_size({"width":1120,"height":800})
                page.evaluate("__outlineCheck.renderer.setPixelRatio(2);__outlineCheck.resizeScene();__outlineCheck.renderLiveScene()")
                page.wait_for_function("__outlineCheck.renderer.domElement.width>=__outlineCheck.renderer.domElement.clientWidth*1.9")
                silhouette(capture(page),capture(page,original=True,geometry=True),2)
                annotated=capture(page,jpeg=True);clean=capture(page,original=True,jpeg=True)
                assert count(mask(annotated,red=True))>200 and count(mask(clean,red=True))==0
                print("PASS resized DPR 2 render keeps the silhouette; selected screenshot contains red while the original evidence image has no outline",flush=True)

                restored=page.evaluate("""()=>{const m=__outlineCheck,a=m.ring.material,b=a.clone();b.transparent=true;b.opacity=.66;b.depthWrite=false;
                    const tex=new m.THREE.DataTexture(new Uint8Array([255,255,255,255,255,0,255,255,255,255,255,255,255,0,255,255]),2,2,m.THREE.RGBAFormat);tex.needsUpdate=true;
                    for(const material of [a,b]){material.alphaMap=tex;material.alphaTest=.4;material.needsUpdate=true;}
                    const total=m.ring.geometry.index.count,half=Math.floor(total/6/2)*6;m.ring.geometry.clearGroups();m.ring.geometry.addGroup(0,half,0);m.ring.geometry.addGroup(half,total-half,1);m.ring.material=[a,b];
                    const original=m.ring.material,background=m.threeScene.background,clear=m.renderer.getClearColor(new m.THREE.Color()),alpha=m.renderer.getClearAlpha(),auto=m.renderer.autoClear;
                    // Three itself increments version during transparent DoubleSide passes.
                    const signature=()=>JSON.stringify(original.map(material=>({uuid:material.uuid,color:material.color.getHex(),opacity:material.opacity,transparent:material.transparent,alphaTest:material.alphaTest,side:material.side,depthWrite:material.depthWrite})));
                    const before=signature();m.renderLiveScene();
                    const cache=[...m.selectionOutline.materials].flatMap(([source,pair])=>[...pair].map(([kind,entry])=>({source,kind,material:entry.material}))),cacheSize=m.selectionOutline.materials.size;
                    for(let index=0;index<20;index++)m.renderLiveScene();
                    const cacheStable=m.selectionOutline.materials.size===cacheSize && cache.every(entry=>m.selectionOutline.materials.get(entry.source)?.get(entry.kind)?.material===entry.material);
                    const okay=m.ring.material===original && signature()===before && m.threeScene.background===background && m.renderer.getRenderTarget()===null && m.renderer.autoClear===auto && m.renderer.getClearAlpha()===alpha && m.renderer.getClearColor(new m.THREE.Color()).equals(clear);
                    const real=m.renderer.render;let threw=false;m.renderer.render=function(scene,camera){if(this.getRenderTarget()===m.selectionOutline.mask)throw Error('forced mask failure');return real.call(this,scene,camera);};
                    try{m.renderLiveScene();}catch(error){threw=error.message==='forced mask failure';}finally{m.renderer.render=real;}
                    return {okay,cacheStable,threw,material:m.ring.material===original,signature:signature()===before,before,after:signature(),background:m.threeScene.background===background,target:m.renderer.getRenderTarget()===null,auto:m.renderer.autoClear===auto,alpha:m.renderer.getClearAlpha()===alpha,color:m.renderer.getClearColor(new m.THREE.Color()).equals(clear)};
                } """)
                assert all(value for value in restored.values() if isinstance(value,bool)),restored
                silhouette(capture(page),capture(page,original=True,geometry=True),2)
                page.evaluate("""()=>{const m=__outlineCheck;m.occluder=new m.THREE.Mesh(new m.THREE.BoxGeometry(.25,1.1,.3),new m.THREE.MeshBasicMaterial({color:'#eae9e3',toneMapped:false}));m.occluder.position.set(.55,1.25,.45);m.threeScene.add(m.occluder);m.renderLiveScene();}""")
                silhouette(capture(page),capture(page,original=True,geometry=True),2)
                resources=page.evaluate("""async scene=>{const m=__outlineCheck,oldRoot=m.selectionOutline.root;
                    const entries=[...m.selectionOutline.materials.values()].flatMap(pair=>[...pair.values()].map(entry=>entry.material));let retired=0;
                    for(const material of entries)material.addEventListener('dispose',()=>retired++);
                    m.state.sceneRevision=scene.revision-1;await m.loadScene(scene);
                    const reloaded=retired===entries.length && m.selectionOutline.root!==oldRoot && m.selectionOutline.root!==null;
                    document.getElementById('clear-selection').click();const cleared=m.selectionOutline.materials.size===0 && m.selectionOutline.root===null;
                    m.selectObject('assembly');m.renderLiveScene();let sourceDisposals=0,cloneDisposals=0;
                    const current=[...m.selectionOutline.materials.values()].flatMap(pair=>[...pair.values()].map(entry=>entry.material));
                    m.selectionOutline.root.traverse(node=>{for(const material of (Array.isArray(node.material)?node.material:node.material?[node.material]:[]))material.addEventListener('dispose',()=>sourceDisposals++);});
                    for(const material of current)material.addEventListener('dispose',()=>cloneDisposals++);
                    let target=0,stroke=0,geometry=0;m.selectionOutline.mask.addEventListener('dispose',()=>target++);m.selectionOutline.stroke.addEventListener('dispose',()=>stroke++);m.selectionOutline.overlay.children[0].geometry.addEventListener('dispose',()=>geometry++);
                    m.selectionOutline.dispose();return {reloaded,cleared,disposed:cloneDisposals===current.length && m.selectionOutline.materials.size===0 && m.selectionOutline.root===null && target===1 && stroke===1 && geometry===1 && sourceDisposals===0};
                }""",scene_before)
                assert all(resources.values()),resources
                assert not errors and not writes and store.scene()==scene_before and asset.read_bytes()==source and not store.state["feedback"],(errors,writes)
                print("PASS material arrays, transparency, texture cutouts and occlusion preserve actual contours; 20 transparent frames reuse cache; failure restores state; clear/reload/dispose release only outline resources; no shader/browser errors or source writes",flush=True)
                browser.close()
        finally:
            server.shutdown();worker.join(5);server.server_close()
    print("ALL SELECTION OUTLINE BROWSER CHECKS PASSED",flush=True)


if __name__=="__main__":
    main()
