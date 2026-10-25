"""Controller orchestration with mocked model/GPU workers, NOT model-quality tests."""
from contextlib import ExitStack, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("run_reconstruction_test", ROOT / "scripts/run_reconstruction.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ObjectPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.world = {"revision": 3, "objects": []}
        self.plan_calls, self.tool_calls, self.build_calls, self.review_calls = [], [], [], []
        self.strategies = ["fuse"]
        self.missing = False
        self.interrupt_builder = False
        self.lose_builder_response = False
        self.hide_queued_feedback = False
        self.reviews = [{"verdict": "accepted", "issues": [], "resolved_issue_ids": []}]
        frames = []
        for index in (1, 7):
            path = self.project / f"observations/train/{index:06d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (20, 20), (80, 100, 120)).save(path)
            frames.append({"index": index, "path": str(path.relative_to(self.project)), "sha256": MODULE.file_hash(path)})
        self.write("observations/manifest.json", {"train": frames, "heldout": []})
        self.runtime = self.project / "runtime.json"
        self.write("runtime.json", {"planner": {"model": "gpt-6-astra", "effort": "high", "codex": "mock-planner"},
                                  "reviewer": {"model": "gpt-6.1-sol", "codex": "mock-reviewer"}})
        outer = self

        class Workbench:
            def __init__(self, url): self.url = url
            def call(self, path):
                if path == "/api/workspace/state":
                    return {"project_dir": str(outer.project), "session_id": "test-session"}
                if path == "/api/sessions/test-session/feedback":
                    return {"items": [] if outer.hide_queued_feedback else [
                        {"feedback_id": f"feedback-{i + 1}", "idempotency_key": row["key"], "note": row["prompt"]}
                        for i, row in enumerate(outer.build_calls)]}
                raise AssertionError(f"Unexpected workbench request: {path}")
            def repair(self, prompt, key):
                outer.build_calls.append({"prompt": prompt, "key": key})
                if outer.lose_builder_response:
                    outer.lose_builder_response = False
                    raise RuntimeError("submission response lost after server persisted feedback")
                return f"feedback-{len(outer.build_calls)}"
            def wait_builder(self, feedback_id):
                if outer.interrupt_builder:
                    outer.interrupt_builder = False
                    raise KeyboardInterrupt("controller interrupted while modeler continues")

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(MODULE.WorldStore, "read", return_value=self.world))
        self.stack.enter_context(patch.object(MODULE, "Workbench", Workbench))
        self.stack.enter_context(patch.object(MODULE, "run_planner", side_effect=self.plan_worker))
        self.stack.enter_context(patch.object(MODULE, "run_tool", side_effect=self.tool_worker))
        self.stack.enter_context(patch.object(MODULE, "review_candidate", side_effect=self.review_worker))

    def write(self, relative, value):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return str(path.relative_to(self.project))

    def options(self, **fields):
        values = {"project": self.project, "runtime": self.runtime, "url": "http://mock-workbench",
                  "max_rounds": 1, "stop_after": "review", "resume": None}
        values.update(fields)
        return SimpleNamespace(**values)

    def execute(self, **fields):
        with redirect_stdout(io.StringIO()):
            return MODULE.run(self.options(**fields))

    def plan_worker(self, project, directory, runtime, previous=None, feedback=None):
        self.plan_calls.append({"previous": previous, "feedback": copy.deepcopy(feedback)})
        strategy = self.strategies[min(len(self.plan_calls) - 1, len(self.strategies) - 1)]
        plan = {"schema_version": 1, "summary": "mock planner inventory", "excluded_objects": [], "unexplained_regions": [],
            "source_manifest_sha256": MODULE.file_hash(project / "observations/manifest.json"),
            "objects": [{"id": "chair", "name": "chair", "category": "chair", "parent_id": None, "kind": "object",
                         "priority": 1, "geometry_strategy": strategy, "segmentation_prompt": "chair",
                         "observations": [{"frame_index": i, "box": [.1, .1, .9, .9], "description": "visible chair"} for i in (1, 7)],
                         "uncertainty": "unobserved back", "acceptance_criteria": ["separately editable"]}]}
        MODULE.validate_plan(project, plan, MODULE.read_json(project / previous) if previous else None)
        return self.write(directory / "plan.json", plan), plan

    def tool_worker(self, name, runtime, project, run_dir, plan_path, output, segments_path=None):
        self.tool_calls.append(name)
        plan = MODULE.read_json(project / plan_path)
        strategy = plan["objects"][0]["geometry_strategy"]
        report = {"schema_version": 1, "plan_sha256": MODULE.file_hash(project / plan_path)}
        if name == "segmentation":
            mask_path = project / output / "mask.png"
            mask_path.parent.mkdir(parents=True)
            Image.new("L", (20, 20), 255).save(mask_path)
            report.update(observations_manifest_sha256=plan["source_manifest_sha256"], objects=[{
                "id": "chair", "status": "missing" if self.missing else "segmented", "reason": "mock SAM result",
                "observations": [] if self.missing else [{"frame_index": i, "mask_path": str(mask_path.relative_to(project)),
                    "mask_sha256": MODULE.file_hash(mask_path), "score": .9, "prompt_method": "text"} for i in (1, 7)]}])
            filename = "segments.json"
        else:
            row = {"id": "chair", "status": "not_required" if strategy == "model" else "insufficient_evidence" if self.missing else "ready",
                   "reason": "mock geometry result", "files": {}}
            if row["status"] == "ready":
                mesh_path = project / output / "chair.obj"
                texture_path = project / output / "chair.png"
                mesh_path.parent.mkdir(parents=True, exist_ok=True)
                mesh_path.write_text("# mock geometry worker artifact, no quality claim\nv 0 0 0\n")
                Image.new("RGB", (4, 4), (80, 100, 120)).save(texture_path)
                row["mesh_path"] = str(mesh_path.relative_to(project))
                row["texture_path"] = str(texture_path.relative_to(project))
                row["files"] = {row["mesh_path"]: MODULE.file_hash(mesh_path), row["texture_path"]: MODULE.file_hash(texture_path)}
            report.update(segments_sha256=MODULE.file_hash(project / segments_path), objects=[row], files=row["files"])
            filename = "geometry.json"
        return self.write(str(Path(output) / filename), report)

    def review_worker(self, options):
        self.review_calls.append(options)
        index = len(self.review_calls) - 1
        review = copy.deepcopy(self.reviews[min(index, len(self.reviews) - 1)])
        relative = f"reconstruction/mock-review-{index + 1}/round_01"
        self.write(relative + "/review.json", review)
        return {"accepted": review["verdict"] == "accepted", "summary": "mock review", "run_id": f"mock-review-{index + 1}",
                "review_directory": relative}

    def test_miss_replans_then_explicit_reference_modeling_reaches_review(self):
        self.strategies = ["fuse", "model"]
        self.missing = True
        status = self.execute()
        self.assertTrue(status["accepted"])
        self.assertEqual(len(self.plan_calls), 2)
        self.assertIn("unresolved", self.plan_calls[1]["feedback"])
        self.assertEqual(self.tool_calls, ["segmentation", "geometry"] * 2)
        self.assertEqual(len(self.build_calls), 1)
        self.assertIn("SAM漏检", self.build_calls[0]["prompt"])
        self.assertEqual(len(self.review_calls), 1)
        plan = MODULE.read_json(self.project / status["plan_path"])
        self.assertEqual(plan["objects"][0]["id"], "chair")
        self.assertEqual(plan["objects"][0]["geometry_strategy"], "model")

    def test_geometry_ready_is_not_quality_accepted(self):
        status = self.execute(stop_after="geometry")
        self.assertEqual(status["stage"], "ready_for_modeling")
        self.assertFalse(status["accepted"])
        self.assertFalse(self.build_calls)
        self.assertFalse(self.review_calls)

    def test_failed_geometry_resume_reuses_completed_plan_and_segmentation(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.missing = missing
                previous_tools = len(self.tool_calls)

                def fail_geometry(name, *args, **kwargs):
                    if name == "geometry":
                        self.tool_calls.append(name)
                        raise RuntimeError("geometry worker interrupted")
                    return self.tool_worker(name, *args, **kwargs)

                with patch.object(MODULE, "run_tool", side_effect=fail_geometry):
                    with self.assertRaisesRegex(RuntimeError, "geometry worker interrupted"):
                        self.execute(stop_after="geometry")
                first = MODULE.read_json(self.project / "object_pipeline/status.json")
                self.assertEqual(first["stage"], "failed")
                self.assertIsNone(first["geometry_path"])
                original_plan_hash = MODULE.file_hash(self.project / first["plan_path"])
                original_segments_hash = MODULE.file_hash(self.project / first["segments_path"])
                plan_count = len(self.plan_calls)
                second = self.execute(resume=first["run_id"], stop_after="geometry")
                self.assertEqual(second["stage"], "needs_revision" if missing else "ready_for_modeling")
                self.assertFalse(second["accepted"])
                self.assertEqual(len(self.plan_calls), plan_count)
                self.assertEqual(self.tool_calls[previous_tools:], ["segmentation", "geometry", "geometry"])
                self.assertEqual(second["plan_path"], first["plan_path"])
                self.assertEqual(second["segments_path"], first["segments_path"])
                self.assertEqual(MODULE.file_hash(self.project / second["plan_path"]), original_plan_hash)
                self.assertEqual(MODULE.file_hash(self.project / second["segments_path"]), original_segments_hash)
                self.assertIn("round_02/geometry_resume/", second["geometry_path"])
                self.assertFalse(self.build_calls)
                self.assertFalse(self.review_calls)

    def test_review_routes_planning_and_segmentation_issues_upstream(self):
        for target in ("planning", "segmentation"):
            with self.subTest(target=target):
                self.plan_calls.clear()
                self.review_calls.clear()
                self.reviews = [{"verdict": "revise", "issues": [{"id": "R001", "target": target}], "resolved_issue_ids": []},
                                {"verdict": "accepted", "issues": [], "resolved_issue_ids": ["R001"]}]
                status = self.execute(max_rounds=2)
                self.assertTrue(status["accepted"])
                self.assertEqual(len(self.plan_calls), 2)
                self.assertEqual(self.plan_calls[1]["feedback"]["issues"][0]["id"], "R001")
                self.assertEqual(self.review_calls[1].previous_review, "reconstruction/mock-review-1/round_01/review.json")

    def test_resume_carries_previous_review_and_does_not_replan_modeling_only_issue(self):
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R017", "target": "modeling"}], "resolved_issue_ids": []},
                        {"verdict": "accepted", "issues": [], "resolved_issue_ids": ["R017"]}]
        first = self.execute()
        self.assertEqual(first["stage"], "needs_revision")
        second = self.execute(resume=first["run_id"])
        self.assertTrue(second["accepted"])
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.review_calls[1].previous_review, first["previous_review"])
        self.assertIn(first["previous_review"], self.build_calls[1]["prompt"])

    def test_resume_cannot_skip_unresolved_missing_objects(self):
        self.missing = True
        first = self.execute()
        self.assertEqual(first["stage"], "needs_revision")
        self.assertFalse(self.build_calls)
        second = self.execute(resume=first["run_id"], stop_after="geometry")
        self.assertEqual(second["stage"], "needs_revision")
        self.assertFalse(second["accepted"])
        self.assertFalse(self.build_calls)
        self.assertEqual(len(self.plan_calls), 4)

    def test_resume_revalidates_existing_mask_bytes(self):
        first = self.execute(stop_after="geometry")
        segments = MODULE.read_json(self.project / first["segments_path"])
        (self.project / segments["objects"][0]["observations"][0]["mask_path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "mask bytes changed"):
            self.execute(resume=first["run_id"])
        self.assertFalse(self.build_calls)

    def test_interrupted_builder_is_waited_and_reviewed_without_duplicate_submission(self):
        self.interrupt_builder = True
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        first = MODULE.read_json(self.project / "object_pipeline/status.json")
        self.assertEqual(first["stage"], "interrupted")
        self.assertFalse(first["accepted"])
        second = self.execute(resume=first["run_id"])
        self.assertTrue(second["accepted"])
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(self.review_calls[0].candidate, first["candidate_path"])
        self.assertEqual(second["round"], first["round"])

    def test_changed_published_world_blocks_resume(self):
        first = self.execute(stop_after="geometry")
        self.world["revision"] += 1
        with self.assertRaisesRegex(ValueError, "published world changed"):
            self.execute(resume=first["run_id"])
        self.assertFalse(self.build_calls)

    def test_lost_submission_response_recovers_persisted_feedback_without_resubmitting(self):
        self.lose_builder_response = True
        with self.assertRaisesRegex(RuntimeError, "submission response lost"):
            self.execute()
        first = MODULE.read_json(self.project / "object_pipeline/status.json")
        self.assertEqual(first["stage"], "failed")
        self.assertIsNone(first["feedback_id"])
        second = self.execute(resume=first["run_id"])
        self.assertTrue(second["accepted"])
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(second["feedback_id"], "feedback-1")
        self.assertEqual(self.review_calls[0].candidate, first["candidate_path"])

    def test_unknown_submission_delivery_stops_instead_of_duplicating_work(self):
        self.lose_builder_response = True
        with self.assertRaisesRegex(RuntimeError, "submission response lost"):
            self.execute()
        first = MODULE.read_json(self.project / "object_pipeline/status.json")
        self.hide_queued_feedback = True
        with self.assertRaises((ValueError, RuntimeError)):
            self.execute(resume=first["run_id"])
        self.assertEqual(len(self.build_calls), 1)
        self.assertFalse(self.review_calls)
        current = MODULE.read_json(self.project / "object_pipeline/status.json")
        self.assertEqual(current["stage"], "failed")
        self.assertFalse(current["accepted"])


if __name__ == "__main__":
    unittest.main()
