"""HTTP context ownership stays valid through scene unloading and retries."""
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from server import make_server
from test_folder_import import glb_bytes


class ProjectUnloadHTTPTests(unittest.TestCase):
    def test_cross_scene_delete_holds_caller_lease_and_hidden_retry_is_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'current'
            project.mkdir()
            scenes = root / 'scenes'
            scenes.mkdir()
            entries = []
            for name in ('A', 'B'):
                directory = scenes / name
                directory.mkdir()
                (directory / 'scene.glb').write_bytes(glb_bytes())
                entries.append({'name': name, 'folder': name, 'glb': name + '/scene.glb'})
            (scenes / 'manifest.json').write_text(json.dumps({'scenes': entries}))
            server = make_server(port=0, project_dir=project, data_dir=root / 'data',
                                 external_review=True, feedback_transport='mcp_events')
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            opener = build_opener(ProxyHandler({}))
            root_token = server.scene_store.browser_token

            def request(path, *, method='GET', token=None, payload=None, origin=None):
                headers = {}
                if token is not None:
                    headers['X-Workspace-Capability'] = token
                if origin is not None:
                    headers['Origin'] = origin
                data = None
                if payload is not None:
                    data = json.dumps(payload).encode()
                    headers['Content-Type'] = 'application/json'
                req = Request(base + path, method=method, headers=headers, data=data)
                try:
                    response = opener.open(req, timeout=10)
                except HTTPError as exc:
                    response = exc
                with response:
                    return response.status, json.load(response)

            entered, release = threading.Event(), threading.Event()
            active = None
            try:
                status, result = request('/api/projects/import-folder', method='POST', token=root_token,
                                         payload={'path': str(scenes), 'request_id': uuid.uuid4().hex})
                self.assertEqual(status, 200, result)
                a, b = [item['project_id'] for item in result['projects']]
                token = server.project_registry.get(b).store.browser_token
                cross_path = f'/p/{b}/api/projects/{a}'
                handler = server.RequestHandlerClass
                original_auth = handler._require_browser_capability
                responses = []

                def held_auth(current):
                    if current.path == cross_path:
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('test request barrier timed out')
                    return original_auth(current)

                with patch.object(handler, '_require_browser_capability', held_auth):
                    active = threading.Thread(target=lambda: responses.append(request(cross_path, method='DELETE', token=token)))
                    active.start()
                    self.assertTrue(entered.wait(3))
                    status, body = request('/api/projects/' + b, method='DELETE', token=root_token)
                    self.assertEqual(status, 409, body)
                    self.assertEqual(len(server.project_registry.contexts()), 3)
                    release.set()
                    active.join(5)
                    self.assertFalse(active.is_alive())
                    self.assertEqual(responses[0][0], 200, responses)
                    self.assertEqual(responses[0][1]['unloaded_project_id'], a)

                status, result = request('/api/projects/' + b, method='DELETE', token=root_token)
                self.assertEqual(status, 200, result)
                retry_path = f'/p/{b}/api/projects/{b}'
                self.assertEqual(request(retry_path, method='DELETE', token=token), (status, result))
                self.assertEqual(request(retry_path, method='DELETE', token='wrong')[0], 403)
                self.assertEqual(request(retry_path, method='DELETE', token=token, origin='https://untrusted.invalid')[0], 403)
                self.assertEqual(request(cross_path, method='DELETE', token=token)[0], 404)
                for endpoint in ('/api/projects', '/api/workspace/state', '/api/scene'):
                    self.assertEqual(request('/p/' + b + endpoint, token=token)[0], 404)
                self.assertEqual(len(server.project_registry.contexts()), 1)
                self.assertTrue((scenes / 'A' / 'scene.glb').exists())
                self.assertTrue((scenes / 'B' / 'scene.glb').exists())
            finally:
                release.set()
                if active is not None:
                    active.join(5)
                server.shutdown()
                worker.join(5)
                server.server_close()


if __name__ == '__main__':
    unittest.main()
