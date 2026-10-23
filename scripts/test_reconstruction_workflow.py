#!/usr/bin/env python3
"""Real Blender + browser check in an isolated synthetic project; no model calls."""
import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from core import SceneStore
from reconstruction_review import accept_candidate, freeze_candidate, read_json, write_json
from world_build import build_world
from world_model import WorldStore
from world_service import WorldService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18780)
    args = parser.parse_args()
    project = Path(tempfile.mkdtemp(prefix="world-workflow-check-"))
    args.output.mkdir(parents=True, exist_ok=True)
    source = read_json(ROOT / "examples/physical_world/scene.json")
    source["views"] = []
    for index, position in enumerate(([3,-4,3],[-3,-4,2.5])):
        z = np.array(position)-np.array([0,0,.5]); z /= np.linalg.norm(z)
        x = np.cross([0,0,1],z); x /= np.linalg.norm(x); y = np.cross(z,x)
        matrix = np.eye(4); matrix[:3,:3] = np.column_stack([x,y,z]); matrix[:3,3] = position
        reference = f"reference_{index}.png"; Image.new("RGB",(480,320),"white").save(project / reference)
        source["views"].append({"id":f"view_{index}","camera_to_world":matrix.tolist(),"reference_image":reference,
            "intrinsics":{"width":480,"height":320,"fx":400,"fy":400,"cx":240,"cy":160}})
    world = WorldStore(project); world.initialize(source)
    store = SceneStore(project / "data"); service = WorldService(project,store)
    initial = service.build(1,render=True)
    for view, rendered in zip(source["views"],initial["render_paths"]):
        Image.open(rendered).convert("RGB").save(project / view["reference_image"])
    build = service.build(1,render=True)
    published = service.publish(1,store.scene()["revision"],build["id"])
    candidate = deepcopy(source); candidate["revision"] = 2
    candidate_build = build_world(project,scene=candidate,render=True)
    write_json(project / "candidate.json",candidate); write_json(project / "build.json",candidate_build)
    frozen = freeze_candidate(project,"candidate.json","build.json",project / "review")
    assert frozen["checks"]["editability"], read_json(project / "review/editability.json")
    write_json(project / "review/synthetic_review.json", {"candidate_id":frozen["candidate_id"],"verdict":"accepted",
        "summary":"Synthetic acceptance gate fixture, not an AI review.",
        "dimensions":{key:"passed" for key in ("appearance","structure","editability")},
        "reviewed_views":frozen["view_ids"],"checked_objects":frozen["object_ids"],"issues":[],"resolved_issue_ids":[]})
    accepted = accept_candidate(project,store,candidate_path="review/candidate.json",review_path="review/synthetic_review.json",
                                expected_revision=1,expected_scene_revision=published["scene"]["revision"])
    assert accepted["world_revision"] == 2
    # Explicit synthetic review fixture exercises the UI, not AI quality judgment.
    status = {"stage":"needs_revision","accepted":False,"round":1,"max_rounds":3,
        "builder":{"model":"gpt-6-astra","effort":"high"},"reviewer":{"model":"gpt-6-sol","effort":"ultra"},
        "summary":"Synthetic browser test fixture; not a real-scene acceptance.","candidate_id":frozen["candidate_id"],
        "object_count":len(source["objects"]),"checks":frozen["checks"],"views":source["views"],"review_directory":"review",
        "issues":[{"id":"test","object_id":"block","view_id":"view_0","severity":"major","region":[0,0,1,1],
                   "evidence":"Synthetic test issue", "acceptance_criterion":"Check actual object transform"}]}
    write_json(project / "reconstruction/status.json",status)
    log = (args.output / "server.log").open("w")
    server = subprocess.Popen([sys.executable,str(ROOT / "backend/server.py"),"--external-review","--port",str(args.port),
        "--project-dir",str(project),"--data-dir",str(project / "data"),"--web-dir",str(ROOT / "web")],stdout=log,stderr=log)
    url = f"http://127.0.0.1:{args.port}"
    op = build_opener(ProxyHandler({}))
    try:
        for _ in range(60):
            if server.poll() is not None: raise RuntimeError("fixture server failed to start")
            try:
                with op.open(url+'/api/workspace/state',timeout=1) as response: json.load(response)
                break
            except OSError: time.sleep(.2)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True,args=['--no-sandbox','--no-proxy-server','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={"width":1440,"height":1000})
            errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(url); page.locator('#flow-open').wait_for(state='visible',timeout=60000)
            page.locator('#flow-open').click()
            page.wait_for_function("document.querySelector('#flow-object').options.length > 0")
            page.wait_for_function("document.querySelector('#flow-render').naturalWidth > 0 && document.querySelector('#flow-reference').naturalWidth > 0")
            page.locator('#flow-issues button').first.click()
            assert page.locator('.flow-region:not([hidden])').count()==2
            page.screenshot(path=str(args.output / "workflow.png"))
            page.locator('#flow-object').select_option('block')
            page.locator('#flow-position input').nth(0).fill('0.2')
            page.locator('#flow-rotation input').nth(2).fill('30')
            page.locator('#flow-apply').click()
            page.wait_for_function("/^(已保存|未完成)/.test(document.querySelector('#flow-edit-result').textContent)",timeout=120000)
            assert page.locator('#flow-edit-result').inner_text().startswith('已保存'), page.locator('#flow-edit-result').inner_text()
            after=world.read(); before={obj['id']:obj for obj in source['objects']}; changed={obj['id']:obj for obj in after['objects']}
            assert after['revision']==3
            assert changed['block']['position']==[.2,.15,.835]
            assert abs(changed['block']['rotation'][2]-math.pi/6)<1e-6
            assert all(before[key]==changed[key] for key in before if key!='block')
            page.screenshot(path=str(args.output / "edited.png"))
            page.locator('#flow-close').click()
            page.locator('#cycles-auto').uncheck()
            page.locator('#cycles-render').click()
            page.wait_for_function("document.querySelector('.cycles-preview-image')?.hidden === false",timeout=60000)
            page.screenshot(path=str(args.output / "edited-cycles.png"))
            page.set_viewport_size({"width":390,"height":844})
            page.locator('#flow-open').click()
            assert page.locator('#reconstruction-dialog').evaluate('e => e.scrollWidth <= e.clientWidth+2')
            page.screenshot(path=str(args.output / "mobile.png"))
            assert not errors, errors
            browser.close()
        result={"project":str(project),"edit_checks":frozen["checks"],"world_revision":after['revision'],
                "acceptance_publication":True,"unchanged_neighbors":True,"cycles_after_edit":True,"page_errors":errors,
                "synthetic_only":True}
        write_json(args.output / "result.json",result); print(json.dumps(result,indent=2))
    finally:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired: server.kill(); server.wait()
        log.close()


if __name__ == '__main__': main()
