"""Quality gates must not publish unreviewed, stale, or incomplete candidates."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import SceneStore
from reconstruction_review import accept_candidate, validate_review, verify_files, review_image, review_status, write_json
from server import make_server
from world_build import file_hash
from world_editor import edit_transform
from world_model import WorldStore, scene_hash

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/physical_world/scene.json"


def review_fixture():
    return {"candidate_id": "frozen", "verdict": "accepted", "summary": "All views inspected",
            "dimensions": {"appearance": "passed", "structure": "passed", "editability": "passed"},
            "reviewed_views": ["front", "back"], "checked_objects": ["bed", "wall"],
            "resolved_issue_ids": [], "issues": []}


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"candidate_id": "frozen", "view_ids": ["front", "back"],
                         "object_ids": ["bed", "wall"], "checks": {"editability": True}}

    def test_independent_dimensions_and_coverage_are_required(self):
        self.assertTrue(validate_review(self.manifest, review_fixture()))
        variants = []
        for field, value in (("candidate_id", "old"), ("reviewed_views", ["front"]), ("checked_objects", ["bed"])):
            review = review_fixture(); review[field] = value; variants.append(review)
        review = review_fixture(); review["dimensions"]["structure"] = "unknown"; variants.append(review)
        for review in variants:
            with self.subTest(review=review), self.assertRaises(ValueError):
                validate_review(self.manifest, review)
        self.manifest["checks"]["editability"] = False
        with self.assertRaises(ValueError):
            validate_review(self.manifest, review_fixture())

    def test_old_issues_cannot_disappear_and_blockers_cannot_be_accepted(self):
        issue = {"id": "bed-back", "object_id": "bed", "view_id": "back", "region": [.1,.1,.8,.8],
                 "severity": "blocking", "evidence": "bed back missing", "acceptance_criterion": "back silhouette complete"}
        previous = {**review_fixture(), "verdict": "revise", "issues": [issue]}
        with self.assertRaises(ValueError):
            validate_review(self.manifest, review_fixture(), previous)
        review = {**review_fixture(), "resolved_issue_ids": ["bed-back"]}
        self.assertTrue(validate_review(self.manifest, review, previous))
        with self.assertRaises(ValueError):
            validate_review(self.manifest, {**review_fixture(), "issues": [issue]})
        self.assertFalse(validate_review(self.manifest, previous))

    def test_issue_references_and_regions_must_be_real(self):
        issue = {"id": "a", "object_id": "bed", "view_id": "back", "region": [.8,.1,.2,.8],
                 "severity": "major", "evidence": "missing", "acceptance_criterion": "complete"}
        review = {**review_fixture(), "verdict": "revise", "issues": [issue]}
        with self.assertRaises(ValueError): validate_review(self.manifest, review)
        issue.update(region=[0,0,1,1], object_id="imaginary")
        with self.assertRaises(ValueError): validate_review(self.manifest, review)

    def test_changed_source_and_outside_project_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp); source = project / "mesh.obj"; source.write_text("mesh")
            files = {source.name: file_hash(source)}
            verify_files(project, files)
            source.write_text("edited after freezing")
            with self.assertRaisesRegex(ValueError, "changed"): verify_files(project, files)
            source.unlink(); source.symlink_to(__file__)
            with self.assertRaisesRegex(ValueError, "in this project"): verify_files(project, files)

    def test_dead_controller_does_not_keep_showing_modeling(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write_json(project / "reconstruction/status.json", {"stage":"modeling", "pid":2147483647})
            self.assertEqual(review_status(project)["stage"],"interrupted")

    def test_failed_review_never_commits_world(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write_json(project / "candidate.json", self.manifest)
            write_json(project / "review.json", {**review_fixture(), "verdict": "revise"})
            with patch.object(WorldStore, "_commit") as commit:
                with self.assertRaisesRegex(ValueError, "not accepted"):
                    accept_candidate(project, SceneStore(project / "data"), candidate_path="candidate.json",
                                     review_path="review.json", expected_revision=1, expected_scene_revision=1)
                commit.assert_not_called()

    def test_review_images_are_bound_to_frozen_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp); image = project / "build/renders/front.png"
            image.parent.mkdir(parents=True); image.write_bytes(b"image")
            write_json(project / "reconstruction/status.json", {"stage":"reviewing", "review_directory":"review"})
            write_json(project / "review/scene.json", {"views":[{"id":"front", "reference_image":"source.png"}]})
            write_json(project / "review/build.json", {"build_dir":str(project / "build")})
            write_json(project / "review/candidate.json", {"scene_path":"review/scene.json", "receipt_path":"review/build.json",
                       "files":{"build/renders/front.png":file_hash(image)}})
            self.assertEqual(review_image(project,"front","render"),image)
            image.write_bytes(b"replaced")
            with self.assertRaisesRegex(ValueError,"changed"): review_image(project,"front","render")
            with self.assertRaises(ValueError): review_image(project,"../secret","render")


class EditorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.project = Path(temp.name); self.store = SceneStore(self.project / "data")
        self.world = WorldStore(self.project)
        self.scene = self.world.initialize(json.loads(EXAMPLE.read_text()))
        self.payload = {"expected_revision":1, "expected_scene_revision":1,
                        "object_id":"block", "position":[1,2,3], "rotation":[0,0,.2]}

    def test_build_failure_does_not_modify_world(self):
        with patch("world_editor.published_build", return_value=({"revision":1},{"scene_hash":scene_hash(self.scene)})), \
             patch("world_editor.build_world", side_effect=ValueError("broken mesh")):
            with self.assertRaisesRegex(ValueError,"broken mesh"):
                edit_transform(self.project,self.store,self.payload)
        self.assertEqual(self.world.read(),self.scene)

    def test_running_review_blocks_edits(self):
        write_json(self.project / "reconstruction/status.json", {"stage":"reviewing"})
        with patch("world_editor.build_world") as build:
            with self.assertRaisesRegex(Exception,"审核正在运行"):
                edit_transform(self.project,self.store,self.payload)
            build.assert_not_called()

    def test_http_transform_requires_browser_capability(self):
        server = make_server(port=0,data_dir=self.project / "server",project_dir=self.project)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            op = build_opener(ProxyHandler({})); url = f"http://127.0.0.1:{server.server_port}"
            for path in ("world-editor", "reconstruction/accept"):
                with self.assertRaises(HTTPError) as caught:
                    op.open(Request(url+'/api/workspace/'+path,data=b'{}',headers={'Content-Type':'application/json'}))
                self.assertEqual(caught.exception.code,403)
            state = json.load(op.open(url+'/api/workspace/state'))
            with self.assertRaises(HTTPError) as caught:
                op.open(Request(url+'/api/workspace/reconstruction/accept',data=b'{}',headers={
                    'Content-Type':'application/json','X-Workspace-Capability':state['browser_capability']}))
            self.assertEqual(caught.exception.code,400)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
