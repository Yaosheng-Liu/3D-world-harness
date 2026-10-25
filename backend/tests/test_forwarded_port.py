"""SSH forwarded ports preserve same-origin protection and generated links."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import make_server


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class ForwardedPortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'index.html').write_text('<html>workbench</html>')
        self.server = make_server(port=0, data_dir=root / 'data', web_dir=root, project_dir=root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path='/api/health', host='localhost:18781', origin=None):
        headers = {'Host': host}
        if origin:
            headers['Origin'] = origin
        request = Request(f'http://127.0.0.1:{self.server.server_port}{path}', headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_page_api_and_local_opener_use_client_port(self):
        for host in ('localhost:18781', '127.0.0.1:18781', '[::1]:18781'):
            with self.subTest(host=host):
                self.assertEqual(self.request('/', host)[0], 200)
                self.assertEqual(self.request(host=host, origin='http://' + host)[0], 200)
                status, headers, _ = self.request('/open', host)
                self.assertEqual(status, 303)
                self.assertTrue(headers['Location'].startswith('http://' + host + '/?session_id='))

    def test_rejects_external_hosts_and_other_browser_origins(self):
        for host in ('evil.test:18781', '127.0.0.1.evil.test:18781', 'evil@localhost:18781',
                     'localhost:0', 'localhost:65536', 'localhost:18781/path'):
            self.assertEqual(self.request(host=host)[0], 403, host)
        for origin in ('https://evil.test', 'http://localhost:18782', 'http://127.0.0.1:18781'):
            self.assertEqual(self.request(origin=origin)[0], 403, origin)

    def test_remote_peer_cannot_use_forwarded_loopback_authority(self):
        original = self.server.get_request
        def remote_request():
            sock, peer = original()
            return sock, ('192.0.2.40', peer[1])
        self.server.get_request = remote_request
        self.assertEqual(self.request()[0], 403)

    def test_forwarding_does_not_bypass_browser_capability(self):
        request = Request(f'http://127.0.0.1:{self.server.server_port}/api/workspace/cycles-preview', data=b'{}',
                          headers={'Host':'localhost:18781', 'Origin':'http://localhost:18781',
                                   'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


if __name__ == '__main__':
    unittest.main()
