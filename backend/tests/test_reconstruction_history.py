"""History must stay bound to its stage and cannot mutate or escape the project."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reconstruction_history import reconstruction_history, history_asset
from reconstruction_plan import planning_status
from world_build import file_hash
from world_model import scene_hash


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.scene = {"revision": 1, "objects": [{"id": "chair"}], "views": [{"id": "front", "reference_image": "reference.png"}]}
        (self.project / "reference.png").write_bytes(b"reference")
        self.write("object_pipeline/run/status.json", {"stage": "modeled", "builder": {"model": "builder", "effort": "high"}})
        self.receipt = self.build("first", self.scene)
        self.write("object_pipeline/run/round_01/model/build_receipt.json", self.receipt)
        self.write("initialization/scene.json", self.scene)
        self.write("initialization/build_receipt.json", self.receipt)

    def write(self, name, value):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def build(self, name, scene):
        base = f"world/builds/{name}"
        self.write(base + "/job.json", {"scene": scene})
        files = {}
        for relative in ("scene.glb", "renders/front.png"):
            path = self.project / base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((name + relative).encode())
            files[relative] = file_hash(path)
        return {"build_dir": base, "glb_path": base + "/scene.glb", "scene_hash": scene_hash(scene),
                "render_paths": [base + "/renders/front.png"], "files": files,
                "asset_hashes": {"reference.png": file_hash(self.project / "reference.png")}}

    def asset(self, descriptor):
        query = parse_qs(urlsplit(descriptor["url"]).query)
        return history_asset(self.project, query["entry"][0], query["asset"][0])

    def test_old_model_and_initialization_stay_bound_after_new_round(self):
        before = reconstruction_history(self.project)
        old = next(entry for entry in before["entries"] if entry["stage"] == "modeling")
        scene = copy.deepcopy(self.scene)
        scene["objects"].append({"id": "lamp"})
        self.write("object_pipeline/run/round_02/model/build_receipt.json", self.build("second", scene))
        after = reconstruction_history(self.project)
        self.assertFalse(after["errors"])
        self.assertEqual(len(after["entries"]), 3)
        self.assertEqual(self.asset(old["views"][0]["render"]).read_bytes(), b"firstrenders/front.png")
        self.assertEqual(old["id"], next(e for e in after["entries"] if e["title"] == old["title"])["id"])
        self.assertFalse(any("_assets" in e for e in after["entries"]))
        self.assertFalse((self.project / "world/scene.json").exists())

    def test_changed_or_unknown_artifacts_are_rejected(self):
        entry = reconstruction_history(self.project)["entries"][0]
        self.asset(entry["mesh"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.asset(entry["mesh"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            history_asset(self.project, "../../secret", "anything")

    def test_outside_symlink_and_partial_run_do_not_hide_valid_history(self):
        with tempfile.TemporaryDirectory() as outside:
            receipt = Path(outside) / "receipt.json"
            receipt.write_text(json.dumps(self.receipt))
            link = self.project / "object_pipeline/run/round_02/model/build_receipt.json"
            link.parent.mkdir(parents=True)
            link.symlink_to(receipt)
            result = reconstruction_history(self.project)
            self.assertEqual(len(result["entries"]), 2)
            self.assertEqual(len(result["errors"]), 1)
            link.unlink()
            link.write_text('{"partial":')
            self.assertEqual(len(reconstruction_history(self.project)["entries"]), 2)

    def test_review_keeps_its_own_candidate_and_issue_evidence(self):
        base = "reconstruction/review-run/round_01"
        scene_path = str(self.write(base + "/scene.json", self.scene).relative_to(self.project))
        receipt_path = str(self.write(base + "/build.json", self.receipt).relative_to(self.project))
        self.write(base + "/candidate.json", {"candidate_id": "frozen", "scene_path": scene_path,
                   "receipt_path": receipt_path, "checks": {"editability": False},
                   "files": {name: file_hash(self.project / name) for name in (scene_path, receipt_path)}})
        self.write(base + "/review.json", {"candidate_id": "frozen", "summary": "needs revision", "issues": [{"id": "shape"}],
                   "verdict": "revise", "dimensions": {"structure": "failed"}, "reviewed_views": ["front"],
                   "checked_objects": ["chair"], "resolved_issue_ids": []})
        result = reconstruction_history(self.project)
        self.assertFalse(result["errors"])
        review = next(e for e in result["entries"] if e["stage"] == "reviewing")
        self.assertEqual(review["issues"], [{"id": "shape"}])
        self.assertEqual(review["candidate_id"], "frozen")
        self.assertEqual(self.asset(review["views"][0]["render"]).read_bytes(), b"firstrenders/front.png")
        self.write(receipt_path, {**self.receipt, "scene_hash": "changed"})
        self.assertTrue(reconstruction_history(self.project)["errors"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.asset(review["views"][0]["render"])

    def test_planning_run_cannot_traverse_or_follow_outside_status(self):
        with self.assertRaises(ValueError):
            planning_status(self.project, "../outside")
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "status.json"
            path.write_text('{"stage":"modeled"}')
            link = self.project / "object_pipeline/escape/status.json"
            link.parent.mkdir(parents=True)
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                planning_status(self.project, "escape")

    def test_http_history_returns_bound_artifacts_and_rejects_unknown_paths(self):
        from server import make_server
        server = make_server(port=0, data_dir=self.project / "agent-data", project_dir=self.project)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = build_opener(ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with opener.open(base + "/api/workspace/reconstruction/history") as response:
                result = json.load(response)
            descriptor = result["entries"][0]["views"][0]["render"]
            with opener.open(base + descriptor["url"]) as response:
                self.assertEqual(response.read(), b"firstrenders/front.png")
            for path in ("/api/workspace/reconstruction/history/asset?entry=../secret&asset=anything",
                         "/api/workspace/object-plan?run=../secret"):
                with self.assertRaises(HTTPError) as error:
                    opener.open(base + path)
                self.assertEqual(error.exception.code, 409)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
