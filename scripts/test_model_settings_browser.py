#!/usr/bin/env python3
"""Model switching over real HTTP/stdio with an isolated fake Codex account."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend/tests'),str(ROOT/'backend'),str(ROOT/'scripts')]
from test_cli_projects import CLIProjectTests
from playwright.sync_api import sync_playwright


def main():
    fixture=CLIProjectTests();fixture.setUp()
    try:
        _, created, _=fixture.create(fixture.payload())
        project=fixture.server.project_registry.get(created['project']['project_id'])
        project.store.replace_scene(1,[{'id':'box','name':'盒子','type':'box','position':[0,0,0],'size':[1,1,1]}])
        before=project.store.workspace();scene=project.store.scene();errors=[]
        base=f'http://127.0.0.1:{fixture.server.server_port}/p/{project.project_id}/'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox','--no-proxy-server','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':1440,'height':900})
            page.on('pageerror',lambda e:errors.append(str(e)))
            def settings():
                page.locator('#projects-dialog-button').click()
                page.locator('#model-settings-button').click()
                page.wait_for_function("!document.getElementById('session-model').disabled")
            page.goto(base);settings()
            assert page.locator('#session-model').input_value()=='fixture-vision'
            assert page.locator('#session-effort').input_value()=='high'
            page.locator('#session-model').select_option('alternate-vision')
            assert page.locator('#session-effort').input_value()==''
            page.locator('#session-effort').select_option('high')
            # Polling must leave unsaved choices intact.
            page.wait_for_timeout(1600)
            assert page.locator('#session-effort').input_value()=='high'
            with page.expect_response(lambda r:r.url.endswith('/api/workspace/model-settings')) as saved:
                page.locator('#save-model-settings').click()
            assert saved.value.status==200
            page.wait_for_function("document.getElementById('model-settings-status').textContent.includes('已保存')")
            assert project.store.workspace()['thread_id']==before['thread_id']
            assert project.store.workspace()['session_id']==before['session_id']
            assert project.store.scene()==scene
            page.screenshot(path='/tmp/model-settings-light.png')
            page.reload();settings()
            assert page.locator('#session-model').input_value()=='alternate-vision'
            assert page.locator('#session-effort').input_value()=='high'
            page.locator('[data-sidebar-view=model] [data-sidebar-home]').click()
            page.locator('#theme-toggle').click()
            page.locator('#model-settings-button').click()
            page.wait_for_function("!document.getElementById('session-model').disabled")
            page.wait_for_timeout(500)
            page.screenshot(path='/tmp/model-settings-dark.png')
            with patch.object(project.gateway,'_model_settings_busy',return_value=True):
                page.locator('#refresh-session-models').click()
                page.wait_for_function("document.getElementById('model-settings-status').textContent.includes('排队')")
                assert page.locator('#session-model').is_disabled()
                assert page.locator('#save-model-settings').is_disabled()
            page.locator('#refresh-session-models').click()
            page.wait_for_function("!document.getElementById('session-model').disabled")
            # An old tab must not overwrite a newer model choice.
            other=project.gateway.state()['model_settings']
            project.gateway.update_model_settings(dict(model='fixture-vision',reasoning_effort='high',expected_settings=other))
            page.locator('#refresh-session-models').click()
            page.wait_for_function("document.getElementById('session-model').value==='fixture-vision'")
            page.set_viewport_size({'width':390,'height':844})
            assert page.locator('#session-model').bounding_box()['width'] < 390
            page.screenshot(path='/tmp/model-settings-mobile.png')
            assert not errors,errors
            browser.close()
        print('PASS model switch, history/scene preserved, reload, pending choices, busy controls, two themes and narrow viewport')
    finally:
        fixture.tearDown()

if __name__=='__main__':main()
