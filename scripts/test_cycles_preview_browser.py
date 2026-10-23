from playwright.sync_api import sync_playwright
from pathlib import Path
import argparse
import json

parser = argparse.ArgumentParser(description='Validate a running world workbench with real Cycles renders.')
parser.add_argument('--url', required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
out=args.output
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-sandbox','--no-proxy-server','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];renders=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 def response(r):
  if r.request.method=='POST' and r.url.endswith('/cycles-preview'):
   data=r.json();data.pop('image',None);renders.append({'status':r.status,**data})
 page.on('response',response)
 def instrument(route):
  r=route.fetch();route.fulfill(response=r,body=r.text()+'\nwindow.__review={state,camera,renderer,controls,THREE,cyclesPreview};')
 page.route('**/app.js',instrument)
 page.goto(args.url,wait_until='domcontentloaded')
 page.wait_for_function('window.__review?.state.workspaceReady && !__review.state.sceneLoading',timeout=60000)
 page.wait_for_function("document.querySelector('.cycles-preview-image')?.hidden===false || document.getElementById('cycles-status').textContent.startsWith('预览失败')",timeout=120000)
 print('status',page.locator('#cycles-status').inner_text(),flush=True)
 print('renders',json.dumps(renders),flush=True)
 page.screenshot(path=str(out/'initial.png'))
 assert page.locator('.cycles-preview-image').is_visible()
 # Navigation invalidates the old preview immediately.
 page.evaluate('__review.camera.position.x += 1; __review.controls.update()')
 page.wait_for_function("document.querySelector('.cycles-preview-image').hidden",timeout=5000)
 page.wait_for_function("!document.querySelector('.cycles-preview-image').hidden",timeout=120000)
 assert len(renders)>=2
 page.locator('#cycles-auto').uncheck()
 assert not page.locator('.cycles-preview-image').is_visible()
 page.locator('#cycles-render').click()
 page.wait_for_function("!document.querySelector('.cycles-preview-image').hidden",timeout=120000)
 assert len(renders)>=3
 # A completed render arriving after camera movement must remain hidden.
 def delayed(route):
  if route.request.method != 'POST':
   route.continue_();return
  response=route.fetch(timeout=120000)
  page.evaluate('__review.camera.position.x += 1; __review.controls.update()')
  route.fulfill(response=response)
 page.route('**/api/workspace/cycles-preview',delayed)
 page.locator('#cycles-render').click()
 page.wait_for_function("!document.getElementById('cycles-render').disabled",timeout=120000)
 assert not page.locator('.cycles-preview-image').is_visible()
 page.unroute('**/api/workspace/cycles-preview',delayed)
 page.locator('#cycles-render').click()
 page.wait_for_function("!document.querySelector('.cycles-preview-image').hidden",timeout=120000)
 assert all(r['status']==200 and r['max_projection_error_px'] < .002 for r in renders),renders
 page.screenshot(path=str(out/'manual.png'))
 assert not errors,errors
 (out/'result.json').write_text(json.dumps({'renders':renders,'page_errors':errors},indent=2))
 print('PASS',json.dumps({'renders':renders,'page_errors':errors}),flush=True)
 browser.close()
