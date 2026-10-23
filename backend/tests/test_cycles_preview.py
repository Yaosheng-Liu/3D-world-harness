"""Preview source identity, request bounds, authorization and stale-render guards."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import APIError, SceneStore
from cycles_preview import published_build, render_preview, validate_request, RENDER_SLOT
from server import make_server
from world_build import file_hash


def request(revision=2):
    return {"scene_revision": revision, "camera_to_world": [[1,0,0,0],[0,1,0,0],[0,0,1,3],[0,0,0,1]],
            "intrinsics": {"width": 640, "height": 480, "fx": 500, "fy": 500, "cx": 320, "cy": 240},
            "near": .01, "far": 100, "time_sec": 0}


class PreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.store = SceneStore(self.project / "data")
        build = self.project / "world/builds/old"
        build.mkdir(parents=True)
        self.blend = build / "scene.blend"
        self.blend.write_bytes(b"frozen blend")
        directory = self.project / "world/receipts/old"
        directory.mkdir(parents=True)
        self.record = {"id": "published", "blend_path": str(self.blend),
                       "files": {"scene.glb": "displayed", "scene.blend": file_hash(self.blend)}}
        (directory / "receipt.json").write_text(json.dumps(self.record))
        self.store.state["scene"] = {"revision": 2, "objects": [{"type": "model", "position": [0,0,0],
            "rotation": [0,0,0], "size": [1,1,1], "metadata": {"sha256": "displayed"}}]}

    def test_uses_published_build_even_with_newer_unpublished_build(self):
        path = self.project / "world/receipts/new"
        path.mkdir()
        newer = {**self.record, "id": "unpublished", "files": {"scene.glb": "other"}}
        (path / "receipt.json").write_text(json.dumps(newer))
        self.assertEqual(published_build(self.project, self.store)[1]["id"], "published")

    def test_rejects_unmatched_or_transformed_display(self):
        for field, value in [("size", [2,2,2]), ("metadata", {"sha256": "other"})]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.store.state["scene"])
                self.store.state["scene"]["objects"][0][field] = value
                with self.assertRaises(APIError):
                    published_build(self.project, self.store)
                self.store.state["scene"] = original

    def test_rejects_invalid_camera_and_resource_limits(self):
        validate_request(request())
        cases = [request() for _ in range(5)]
        cases[0]["camera_to_world"][0][0] = 2
        cases[1]["camera_to_world"][1][3] = float("nan")
        cases[2]["intrinsics"]["width"] = 10000
        cases[3]["far"] = .001
        cases[4]["blend_path"] = "/some/other/scene.blend"
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(APIError):
                validate_request(payload)

    def test_stale_revision_changed_source_and_busy_do_not_start_blender(self):
        with patch("cycles_preview.subprocess.run") as run:
            with self.assertRaises(APIError):
                render_preview(self.project, self.store, request(1))
            with RENDER_SLOT, self.assertRaises(APIError) as caught:
                render_preview(self.project, self.store, request())
            self.assertEqual(caught.exception.status, 429)
            self.blend.write_bytes(b"changed")
            with self.assertRaises(APIError):
                render_preview(self.project, self.store, request())
            run.assert_not_called()

    def test_scene_change_during_render_discards_result_and_releases_slot(self):
        def render(*args, **kwargs):
            self.store.state["scene"]["revision"] += 1
            return subprocess.CompletedProcess(args, 0)
        with patch("cycles_preview.subprocess.run", side_effect=render), self.assertRaises(APIError) as caught:
            render_preview(self.project, self.store, request())
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(RENDER_SLOT.locked())

    def test_timeout_releases_slot(self):
        with patch("cycles_preview.subprocess.run", side_effect=subprocess.TimeoutExpired("blender", 90)):
            with self.assertRaises(APIError) as caught:
                render_preview(self.project, self.store, request())
        self.assertEqual(caught.exception.status, 504)
        self.assertFalse(RENDER_SLOT.locked())

    def test_http_render_requires_browser_capability(self):
        server = make_server(port=0, project_dir=self.project, data_dir=self.project / "http", enable_codex=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            op = build_opener(ProxyHandler({}))
            url = f"http://127.0.0.1:{server.server_port}/api/workspace/cycles-preview"
            self.assertFalse(json.load(op.open(url))["available"])
            req = Request(url, data=json.dumps(request()).encode(), headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                op.open(req)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
