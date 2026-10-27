#!/usr/bin/env python3
"""Browser approval-mode checks with synthetic requests; never approves real agent work."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
from threading import Lock, Thread

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    state = {'delivery_mode': 'appserver', 'project_id': 'project', 'session_id': 'session',
             'thread_id': 'thread', 'approvals': []}
    posts, errors = [], []
    lock = Lock()

    def request(key, kind='item/commandExecution/requestApproval', **fields):
        return {'approval_id': key, 'kind': kind, 'source_thread_id': 'thread', **fields}

    def tool_request(key, tool):
        return request(key, 'mcpServer/elicitation/request', details={'serverName': 'scene_feedback', 'mode': 'form',
            '_meta': {'codex_approval_kind': 'mcp_tool_call'}, 'requestedSchema': {'type': 'object', 'properties': {}},
            'message': f'Allow the scene_feedback MCP server to run tool "{tool}"?'})

    with tempfile.TemporaryDirectory(prefix='approval-browser-') as temp:
        root = Path(temp)
        (root / 'web').symlink_to(ROOT / 'web', target_is_directory=True)
        control = re.search(r'<section class="sidebar-approval".*?</section>', (ROOT / 'web/index.html').read_text()).group()
        (root / 'fixture.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>操作审批</title>
<link rel="stylesheet" href="/web/styles.css"><link rel="stylesheet" href="/web/workspace-sidebar.css">
<main style="width:320px;max-width:100%;margin:24px auto;padding:12px 0 24px;background:var(--paper);border:1px solid var(--line);border-radius:16px">
''' + control + '''</main><script type="module">
import {setupApprovalSettings} from '/web/approval-settings.js';
const api=async(path,options={})=>{const r=await fetch(path,{method:options.method || 'GET',headers:{'Content-Type':'application/json'},body:options.body?JSON.stringify(options.body):undefined});if(!r.ok)throw Error('模拟响应中断');return r.json();};
window.settings=setupApprovalSettings({api,onWorkspace:s=>settings.update(s)});
window.refresh=async()=>settings.update(await api('/api/workspace/state'));
await refresh();window.ready=true;
</script></html>''')

        class Handler(SimpleHTTPRequestHandler):
            def reply(self, value, code=200):
                body = json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/api/workspace/state':
                    with lock:
                        return self.reply(state)
                return super().do_GET()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                key = self.path.split('/')[-2]
                with lock:
                    posts.append((key, body))
                    state['approvals'] = [r for r in state['approvals'] if r['approval_id'] != key]
                    self.reply({'status': 'responded'}, 503 if key == 'uncertain' else 200)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(root)))
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--no-proxy-server'])
                context = browser.new_context(viewport={'width': 1000, 'height': 700})
                context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
                page = context.new_page()
                url = f'http://127.0.0.1:{server.server_port}/fixture.html'
                state['approvals'] = [request('command'), request('file', 'item/fileChange/requestApproval'),
                    tool_request('preview', 'workspace_preview_world'), tool_request('publish', 'workspace_publish_world'),
                    request('form', 'mcpServer/elicitation/request', details={'mode': 'form', 'requestedSchema': {'type': 'object', 'properties': {}}}),
                    request('question', 'item/tool/requestUserInput'), request('permissions', 'item/permissions/requestApproval'),
                    request('large', details_truncated=True), request('other-thread', source_thread_id='other')]
                page.goto(url)
                page.wait_for_function('window.ready')
                assert page.locator('#approval-mode').input_value() == 'manual'
                assert not posts
                page.locator('#approval-mode').select_option('automatic')
                page.wait_for_function("document.querySelector('#approval-mode-status').textContent.includes('最近一次')")
                assert [key for key, _ in posts] == ['command', 'file', 'preview']
                assert all(body == {'decision': 'accept'} for _, body in posts)
                page.screenshot(path=str(args.output / 'automatic-desktop.png'))
                page.reload()
                page.wait_for_function('window.ready')
                assert page.locator('#approval-mode').input_value() == 'automatic'
                tab = context.new_page()
                tab.goto(url)
                tab.wait_for_function('window.ready')
                with lock:
                    state['approvals'].append(request('shared'))
                page.evaluate('refresh()')
                tab.evaluate('refresh()')
                page.wait_for_function("!document.querySelector('#approval-mode-status').textContent.includes('暂停')")
                for _ in range(20):
                    if any(key == 'shared' for key, _ in posts):
                        break
                    page.wait_for_timeout(50)
                assert [key for key, _ in posts].count('shared') == 1
                tab.locator('#approval-mode').select_option('manual')
                page.wait_for_function("document.querySelector('#approval-mode').value==='manual'")
                with lock:
                    state['approvals'].append(request('manual'))
                page.evaluate('refresh()')
                page.wait_for_timeout(150)
                assert not any(key == 'manual' for key, _ in posts)
                with lock:
                    state['approvals'] = [request('uncertain'), request('after-error')]
                page.evaluate('refresh()')
                page.locator('#approval-mode').select_option('automatic')
                page.wait_for_function("document.querySelector('#approval-mode-status').textContent.includes('自动确认已暂停')")
                assert page.locator('#approval-mode').input_value() == 'manual'
                page.reload()
                page.wait_for_function('window.ready')
                assert [key for key, _ in posts].count('uncertain') == 1
                assert not any(key == 'after-error' for key, _ in posts)
                # A different scene session has its own default; external connectors stay manual.
                page.evaluate("settings.update({delivery_mode:'appserver',project_id:'project',session_id:'other',thread_id:'thread',approvals:[]})")
                assert page.locator('#approval-mode').input_value() == 'manual'
                page.set_viewport_size({'width': 390, 'height': 650})
                page.screenshot(path=str(args.output / 'manual-mobile.png'))
                page.evaluate("settings.update({delivery_mode:'external',session_id:'other',approvals:[]})")
                assert page.locator('#approval-settings').is_hidden()
                assert not errors, errors
                browser.close()
            (args.output / 'result.json').write_text(json.dumps({'posts': [key for key, _ in posts], 'errors': errors}, indent=2))
            print(json.dumps({'passed': True, 'checks': ['manual', 'automatic', 'scope', 'refresh', 'multi-tab', 'uncertain-response', 'mobile']}))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
