"""Quality gates must not publish unreviewed, stale, or incomplete candidates."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
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


class ReviewerRepairLoopTests(unittest.TestCase):
    def test_segmentation_category_repairs_the_same_builder_without_reinitialization(self):
        script = Path(__file__).resolve().parents[2] / "scripts/review_reconstruction.py"
        spec = importlib.util.spec_from_file_location("review_reconstruction_loop_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            repairs, reviews, waited = [], [], []
            state = {"project_dir": str(project), "model_settings": {"model": "gpt-6-astra", "reasoning_effort": "high"},
                     "thread_id": "same-modeler-thread", "scene_revision": 1}

            class Workbench:
                def __init__(self, url): pass
                def call(self, path, payload=None):
                    return {"world_revision": 2, "scene": {"revision": 2}} if payload else state
                def repair(self, note, key):
                    repairs.append(note)
                    return "repair-feedback"
                def wait_builder(self, feedback_id):
                    waited.append(feedback_id)

            def freeze(project, candidate, receipt, output, **kwargs):
                output.mkdir(parents=True)
                manifest = {"candidate_id": str(len(reviews)), "workflow_version": 2,
                    "plan_path": "plan.json", "segments_path": "segments.json", "geometry_path": "geometry.json",
                    "modeling_report_path": "modeling_report.json", "inventory_path": "inventory.json",
                    "object_ids": ["bed", "wall"], "required_object_ids": ["bed", "wall"], "view_ids": ["front", "back"],
                    "checks": {"inventory": True}, "files": {}, "previous_review_path": kwargs.get("previous_review")}
                if manifest["previous_review_path"] is not None:
                    manifest["previous_review_path"] = str(manifest["previous_review_path"])
                write_json(output / "candidate.json", manifest)
                write_json(output / "scene.json", {"views": [{"id": key} for key in manifest["view_ids"]]})
                write_json(output / "inspection.json", {"inspection_paths": []})
                write_json(output / "editability.json", {"inspection_paths": []})
                return manifest

            def critic(command, **kwargs):
                path = Path(command[command.index("--output-last-message") + 1])
                review = {**review_fixture(), "candidate_id": str(len(reviews))}
                if not reviews:
                    review.update(verdict="revise", issues=[{"id": "boundary", "object_id": "bed", "view_id": "front",
                        "category": "segmentation", "region": [0, 0, 1, 1], "severity": "major",
                        "evidence": "neighbor is fused into bed", "acceptance_criterion": "separate editable geometry"}])
                else:
                    review["resolved_issue_ids"] = ["boundary"]
                write_json(path, review)
                kwargs["stdout"].write('{"type":"thread.started","thread_id":"critic"}\n')
                reviews.append(review)
                return SimpleNamespace(returncode=0)

            options = SimpleNamespace(project=project, url="mock", candidate="candidate.json", receipt="receipt.json",
                feedback_id=None, previous_review=None, review_model="gpt-6.1-sol", requested_review_model="gpt-6.1-sol",
                max_rounds=2, blender="mock", codex="mock", plan="plan.json", segments="segments.json", geometry="geometry.json")
            with patch.object(module, "Workbench", Workbench), patch.object(module.WorldStore, "read", return_value={"revision": 1}), \
                    patch.object(module, "freeze_candidate", side_effect=freeze), patch.object(module.subprocess, "run", side_effect=critic):
                result = module.run(options)
            self.assertTrue(result["accepted"])
            self.assertEqual(len(repairs), 1)
            self.assertEqual(len(reviews), 2)
            self.assertEqual(waited, ["repair-feedback"])
            self.assertIn("逐物体精细重建", repairs[0])
            self.assertIn("modeling_report.json", repairs[0])


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
        self.modeling_report = {"schema_version": 1, "summary": "Candidate mapping for synthetic review tests",
                               "objects": [{"plan_id": key, "scene_ids": [key], "resolution": "modeled",
                                            "reason": "Modeled independent object from the original reference",
                                            "frame_indices": [0]} for key in ("bed", "wall")], "regions": []}
        self.output = self.project / "frozen"; self.output.mkdir()
        self.bind_inputs()

    def bind_inputs(self):
        write_json(self.project / "plan.json", self.plan)
        self.segments["plan_sha256"] = file_hash(self.project / "plan.json")
        write_json(self.project / "segments.json", self.segments)
        self.geometry.update(plan_sha256=file_hash(self.project / "plan.json"),
                             segments_sha256=file_hash(self.project / "segments.json"))
        write_json(self.project / "geometry.json", self.geometry)
        self.modeling_report["plan_sha256"] = file_hash(self.project / "plan.json")
        write_json(self.project / "modeling_report.json", self.modeling_report)

    def freeze(self):
        files = {}
        bound, passed = freeze_plan_evidence(self.project, self.scene, self.output, files,
                                             "plan.json", "segments.json", "geometry.json",
                                             modeling_report_path="modeling_report.json")
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
        self.assertIn("modeling_report.json", manifest["files"])
        self.assertIn(manifest["modeling_report_path"], manifest["files"])
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
        self.modeling_report["objects"][1].update(scene_ids=[], resolution="unresolved", reason="Wall is still missing")
        self.bind_inputs()
        manifest = self.freeze()
        self.assertFalse(manifest["checks"]["inventory"])
        with self.assertRaisesRegex(ValueError, "deterministic checks"):
            validate_review(manifest, self.planned_review())
        issue = {"id": "missing-wall", "object_id": "wall", "view_id": "source_0", "category": "inventory",
                 "region": [0, 0, 1, 1], "severity": "blocking", "evidence": "planned wall absent",
                 "acceptance_criterion": "wall has its own editable object"}
        review = {**self.planned_review(), "verdict": "revise", "issues": [issue]}
        self.assertFalse(validate_review(manifest, review))
        issue.update(object_id="scene", view_id="source_2", evidence="unlisted lamp visible in original")
        self.assertFalse(validate_review(manifest, review))

    def test_initial_regions_and_hierarchy_can_be_resolved_by_the_builder(self):
        self.plan["objects"][1]["parent_id"] = "bed"
        self.plan["unexplained_regions"] = [{"frame_index": 2, "box": [.1,.1,.3,.3], "description": "unexplained wall detail"}]
        self.modeling_report["regions"] = [{"region_index": 0, "scene_ids": [], "resolution": "unresolved",
                                            "reason": "Wall detail is still missing", "frame_indices": [2]}]
        self.bind_inputs()
        manifest = self.freeze()
        self.assertFalse(manifest["checks"]["inventory"])
        self.modeling_report["regions"][0].update(scene_ids=["wall"], resolution="modeled", reason="Wall detail is in the independent wall object")
        self.modeling_report["objects"][1]["reason"] = "Original images show an independent wall, not a bed child"
        self.bind_inputs()
        manifest = self.freeze()
        self.assertTrue(manifest["checks"]["inventory"])

    def test_modeling_report_bytes_are_bound_to_the_frozen_candidate(self):
        manifest = self.freeze()
        self.modeling_report["summary"] = "Changed after freezing"
        write_json(self.project / "modeling_report.json", self.modeling_report)
        with self.assertRaisesRegex(ValueError, "changed"):
            verify_files(self.project, manifest["files"])

    def test_planned_issues_require_categories_and_preserve_cross_run_issue_ids(self):
        from jsonschema import ValidationError
        manifest = self.freeze()
        issue = {"id": "mask-leak", "object_id": "bed", "view_id": "source_0", "category": "segmentation",
                 "region": [0, 0, 1, 1], "severity": "major", "evidence": "mask contains neighbor",
                 "acceptance_criterion": "neighbor excluded"}
        previous = {**self.planned_review(), "verdict": "revise", "issues": [issue]}
        with self.assertRaisesRegex(ValueError, "every previous issue"):
            validate_review(manifest, self.planned_review(), previous)
        resolved = {**self.planned_review(), "resolved_issue_ids": ["mask-leak"]}
        self.assertTrue(validate_review(manifest, resolved, previous))
        self.assertFalse(validate_review(manifest, previous, previous))
        del issue["category"]
        with self.assertRaises(ValidationError):
            validate_review(manifest, previous)

    def test_historical_target_schema_is_only_accepted_for_old_manifests(self):
        from jsonschema import ValidationError
        manifest = self.freeze()
        self.assertEqual(manifest["workflow_version"], 2)
        issue = {"id": "old-mask", "object_id": "bed", "view_id": "source_0", "target": "segmentation",
                 "region": [0, 0, 1, 1], "severity": "major", "evidence": "neighbor included",
                 "acceptance_criterion": "neighbor is independently modeled"}
        old_review = {**self.planned_review(), "verdict": "revise", "issues": [issue]}
        with self.assertRaises(ValidationError):
            validate_review(manifest, old_review)
        historical = {key: value for key, value in manifest.items() if key != "workflow_version"}
        self.assertFalse(validate_review(historical, old_review))
        self.assertFalse(validate_review({**historical, "workflow_version": 1}, old_review))
        migrated = deepcopy(old_review)
        migrated["issues"][0].pop("target")
        migrated["issues"][0]["category"] = "segmentation"
        self.assertFalse(validate_review(manifest, migrated, old_review))
        with self.assertRaisesRegex(ValueError, "workflow version"):
            validate_review({**manifest, "workflow_version": 99}, migrated)

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
