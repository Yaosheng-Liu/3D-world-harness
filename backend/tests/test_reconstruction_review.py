"""Quality gates must not publish unreviewed, stale, or incomplete candidates."""
from copy import deepcopy
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
from reconstruction_review import (accept_candidate, validate_review, verify_files, review_image, review_status,
                                   write_json, freeze_candidate, freeze_plan_evidence)
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


class PlannedReviewTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        rows = []
        for index in range(3):
            path = self.project / f"observations/train/{index:06d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (20, 12), (index * 50, 120, 90)).save(path)
            rows.append({"index": index, "path": str(path.relative_to(self.project)), "sha256": file_hash(path)})
        write_json(self.project / "observations/manifest.json", {"train": rows, "heldout": []})
        observation = lambda index: {"frame_index": index, "box": [0, 0, 1, 1], "description": "visible source evidence"}
        self.plan = {"schema_version": 1, "source_manifest_sha256": file_hash(self.project / "observations/manifest.json"),
                     "summary": "Synthetic inventory for acceptance gates", "unexplained_regions": [], "excluded_objects": [],
                     "objects": [{"id": key, "name": key, "category": key, "parent_id": None,
                                  "kind": "object", "priority": 1, "geometry_strategy": "model",
                                  "segmentation_prompt": key, "observations": [observation(0)],
                                  "uncertainty": "hidden surfaces estimated", "acceptance_criteria": ["independent edit"]}
                                 for key in ("bed", "wall")]}
        mask = self.project / "mask.png"; Image.new("L", (20, 12), 255).save(mask)
        self.segments = {"observations_manifest_sha256": self.plan["source_manifest_sha256"],
                         "objects": [{"id": "bed", "status": "segmented", "observations": [
                             {"frame_index": 0, "mask_path": "mask.png", "mask_sha256": file_hash(mask)}]},
                                     {"id": "wall", "status": "missing", "observations": []}]}
        self.geometry = {"observations_manifest_sha256": self.plan["source_manifest_sha256"],
                         "objects": [{"id": key, "status": "not_required", "files": {}} for key in ("bed", "wall")],
                         "files": {}, "input_files": {}}
        self.scene = {"objects": [{"id": "bed", "parent_id": None}, {"id": "wall", "parent_id": None}]}
        self.output = self.project / "frozen"; self.output.mkdir()
        self.bind_inputs()

    def bind_inputs(self):
        write_json(self.project / "plan.json", self.plan)
        self.segments["plan_sha256"] = file_hash(self.project / "plan.json")
        write_json(self.project / "segments.json", self.segments)
        self.geometry.update(plan_sha256=file_hash(self.project / "plan.json"),
                             segments_sha256=file_hash(self.project / "segments.json"))
        write_json(self.project / "geometry.json", self.geometry)

    def freeze(self):
        files = {}
        bound, passed = freeze_plan_evidence(self.project, self.scene, self.output, files,
                                             "plan.json", "segments.json", "geometry.json")
        manifest = {"candidate_id": "frozen", "object_ids": [obj["id"] for obj in self.scene["objects"]],
                    "view_ids": ["front", "back"], "checks": {"editability": True, "inventory": passed},
                    "files": files, **bound}
        return manifest

    def planned_review(self):
        return {**review_fixture(), "reviewed_views": ["front", "back", "source_0", "source_1", "source_2"]}

    def test_freeze_binds_all_original_views_even_outside_inventory_boxes(self):
        manifest = self.freeze()
        self.assertTrue(manifest["checks"]["inventory"])
        self.assertEqual(manifest["source_view_ids"], ["source_0", "source_1", "source_2"])
        self.assertIn("observations/train/000002.png", manifest["files"])
        self.assertIn("mask.png", manifest["files"])
        self.assertEqual(len(manifest["source_inspection_paths"]), 1)
        self.assertTrue(validate_review(manifest, self.planned_review()))
        # Viewing only existing render cameras must not stand in for original-image coverage.
        with self.assertRaisesRegex(ValueError, "all views"):
            validate_review(manifest, review_fixture())
        (self.project / "observations/train/000002.png").write_bytes(b"changed unseen region")
        with self.assertRaisesRegex(ValueError, "changed"):
            verify_files(self.project, manifest["files"])

    def test_missing_planned_object_blocks_acceptance_and_can_be_reported(self):
        self.scene["objects"].pop()
        manifest = self.freeze()
        self.assertFalse(manifest["checks"]["inventory"])
        with self.assertRaisesRegex(ValueError, "deterministic checks"):
            validate_review(manifest, self.planned_review())
        issue = {"id": "missing-wall", "object_id": "wall", "view_id": "source_0", "target": "planning",
                 "region": [0, 0, 1, 1], "severity": "blocking", "evidence": "planned wall absent",
                 "acceptance_criterion": "wall has its own editable object"}
        review = {**self.planned_review(), "verdict": "revise", "issues": [issue]}
        self.assertFalse(validate_review(manifest, review))
        issue.update(object_id="scene", view_id="source_2", evidence="unlisted lamp visible in original")
        self.assertFalse(validate_review(manifest, review))

    def test_unexplained_regions_and_wrong_hierarchy_cannot_pass_inventory(self):
        self.plan["objects"][1]["parent_id"] = "bed"
        self.plan["unexplained_regions"] = [{"frame_index": 2, "box": [.1,.1,.3,.3], "description": "unexplained lamp"}]
        self.bind_inputs()
        manifest = self.freeze()
        inventory = json.loads((self.project / manifest["inventory_path"]).read_text())
        self.assertEqual(inventory["hierarchy_mismatches"], ["wall"])
        self.assertEqual(inventory["unexplained_regions"], 1)
        self.assertFalse(manifest["checks"]["inventory"])

    def test_planned_issues_require_routing_and_preserve_cross_run_issue_ids(self):
        from jsonschema import ValidationError
        manifest = self.freeze()
        issue = {"id": "mask-leak", "object_id": "bed", "view_id": "source_0", "target": "segmentation",
                 "region": [0, 0, 1, 1], "severity": "major", "evidence": "mask contains neighbor",
                 "acceptance_criterion": "neighbor excluded"}
        previous = {**self.planned_review(), "verdict": "revise", "issues": [issue]}
        with self.assertRaisesRegex(ValueError, "every previous issue"):
            validate_review(manifest, self.planned_review(), previous)
        resolved = {**self.planned_review(), "resolved_issue_ids": ["mask-leak"]}
        self.assertTrue(validate_review(manifest, resolved, previous))
        self.assertFalse(validate_review(manifest, previous, previous))
        del issue["target"]
        with self.assertRaises(ValidationError):
            validate_review(manifest, previous)

    def test_plan_mask_geometry_hash_mismatches_are_rejected_before_review(self):
        for kind in ("plan", "segments", "mask", "geometry_asset", "absolute_path", "outside_path"):
            with self.subTest(kind=kind):
                self.bind_inputs()
                geometry = deepcopy(self.geometry)
                if kind in {"plan", "segments"}:
                    geometry[kind + "_sha256"] = "different"
                elif kind == "mask":
                    segments = deepcopy(self.segments)
                    segments["objects"][0]["observations"][0]["mask_sha256"] = "different"
                    write_json(self.project / "segments.json", segments)
                    geometry["segments_sha256"] = file_hash(self.project / "segments.json")
                elif kind == "geometry_asset":
                    geometry["files"] = {"mask.png": "different"}
                elif kind == "absolute_path":
                    geometry["files"] = {str(self.project / "mask.png"): file_hash(self.project / "mask.png")}
                else:
                    geometry["files"] = {"../outside.png": "different"}
                write_json(self.project / "geometry.json", geometry)
                with self.assertRaises(ValueError):
                    self.freeze()

    def test_source_review_images_are_reference_only_and_hash_bound(self):
        manifest = self.freeze()
        write_json(self.output / "candidate.json", manifest)
        write_json(self.project / "reconstruction/status.json", {"stage": "needs_revision", "review_directory": "frozen"})
        self.assertEqual(review_image(self.project, "source_2", "reference"), self.project / "observations/train/000002.png")
        with self.assertRaisesRegex(ValueError, "no corresponding"):
            review_image(self.project, "source_2", "render")
        (self.project / "observations/train/000002.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            review_image(self.project, "source_2", "reference")

    def test_partial_planning_input_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "supplied together"):
            freeze_candidate(self.project, "candidate.json", "build.json", self.project / "other", plan_path="plan.json")


if __name__ == "__main__":
    unittest.main()
