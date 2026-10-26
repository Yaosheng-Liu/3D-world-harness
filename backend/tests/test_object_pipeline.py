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
        self.order = []
        self.effort = "high"
        self.strategies = ["fuse"]
        self.missing = False
        self.geometry_status = None
        self.unexplained = []
        self.interrupt_builder = False
        self.lose_builder_response = False
        self.hide_queued_feedback = False
        self.report_modes = []
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
                    return {"project_dir": str(outer.project), "session_id": "test-session",
                            "model_settings": {"model": "gpt-6-astra", "reasoning_effort": outer.effort}}
                if path == "/api/sessions/test-session/feedback":
                    return {"items": [] if outer.hide_queued_feedback else [
                        {"feedback_id": f"feedback-{i + 1}", "idempotency_key": row["key"], "note": row["prompt"]}
                        for i, row in enumerate(outer.build_calls)]}
                raise AssertionError(f"Unexpected workbench request: {path}")
            def repair(self, prompt, key):
                outer.order.append("repair")
                outer.build_calls.append({"prompt": prompt, "key": key})
                if outer.lose_builder_response:
                    outer.lose_builder_response = False
                    raise RuntimeError("submission response lost after server persisted feedback")
                return f"feedback-{len(outer.build_calls)}"
            def wait_builder(self, feedback_id, **kwargs):
                if outer.interrupt_builder:
                    outer.interrupt_builder = False
                    raise KeyboardInterrupt("controller interrupted while modeler continues")
                outer.complete_builder()

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
        plan = {"schema_version": 1, "summary": "mock planner inventory", "excluded_objects": [], "unexplained_regions": copy.deepcopy(self.unexplained),
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
            if self.geometry_status:
                row.update(status=self.geometry_status, reason="UV refinement exceeded its explicit face limit")
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
        self.order.append("review")
        self.assertFalse(MODULE.read_json(self.project / "object_pipeline/status.json")["accepted"])
        self.review_calls.append(options)
        index = len(self.review_calls) - 1
        review = copy.deepcopy(self.reviews[min(index, len(self.reviews) - 1)])
        relative = f"reconstruction/mock-review-{index + 1}/round_01"
        self.write(relative + "/review.json", review)
        return {"accepted": review["verdict"] == "accepted" and getattr(options, "publish_after_review", True), "summary": "mock review", "run_id": f"mock-review-{index + 1}",
                "review_directory": relative}

    def complete_builder(self):
        status = MODULE.read_json(self.project / "object_pipeline/status.json")
        candidate = Path(status["candidate_path"])
        self.write(candidate, {"objects": [{"id": "chair", "parent_id": None}]})
        report_path = candidate.parent / "modeling_report.json"
        mode = self.report_modes.pop(0) if self.report_modes else "valid"
        if mode == "missing":
            return
        if mode == "json":
            (self.project / report_path).write_text("{unfinished")
            return
        disposition = {"scene_ids": ["chair"], "resolution": "modeled",
                       "reason": "Built from the original training image", "frame_indices": [1]}
        report = {"schema_version": 1, "summary": "mock builder report",
                  "plan_sha256": MODULE.file_hash(self.project / status["plan_path"]),
                  "objects": [{"plan_id": "chair", **disposition}],
                  "regions": [{"region_index": index, **disposition} for index in range(len(self.unexplained))]}
        if mode == "schema":
            del report["summary"]
        elif mode == "mapping":
            report["objects"][0]["scene_ids"] = ["absent"]
        elif mode == "incomplete":
            report["objects"] = []
        elif mode == "unresolved":
            report["objects"][0]["resolution"] = "unresolved"
        self.write(report_path, report)

    def test_report_format_errors_return_to_builder_without_reinitializing_or_faking_review(self):
        for mode in ("missing", "json", "schema", "mapping"):
            with self.subTest(mode=mode):
                self.plan_calls.clear()
                self.tool_calls.clear()
                self.build_calls.clear()
                self.review_calls.clear()
                self.report_modes = [mode, "valid"]
                status = self.execute(max_rounds=2)
                self.assertTrue(status["accepted"])
                self.assertEqual(len(self.plan_calls), 1)
                self.assertEqual(self.tool_calls, ["segmentation", "geometry"])
                self.assertEqual(len(self.build_calls), 2)
                self.assertEqual(len(self.review_calls), 1)
                self.assertIsNone(self.review_calls[0].previous_review)
                self.assertIn("builder_diagnostic.json", self.build_calls[1]["prompt"])
                diagnostic = self.project / "object_pipeline" / status["run_id"] / "round_01/model/builder_diagnostic.json"
                data = MODULE.read_json(diagnostic)
                self.assertEqual(data["kind"], "modeling_report")
                self.assertNotIn("verdict", data)
                self.assertLess(len(data["message"]), 500)

    def test_report_diagnostic_resume_starts_next_model_round_and_preserves_real_review(self):
        self.report_modes = ["valid", "missing", "valid"]
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R001", "category": "geometry"}], "resolved_issue_ids": []},
                        {"verdict": "accepted", "issues": [], "resolved_issue_ids": ["R001"]}]
        first = self.execute(max_rounds=2)
        self.assertEqual(first["stage"], "needs_revision")
        self.assertEqual(first["checked_feedback_id"], "feedback-2")
        self.assertEqual(first["reviewed_feedback_id"], "feedback-1")
        second = self.execute(resume=first["run_id"])
        self.assertTrue(second["accepted"])
        self.assertEqual(second["round"], first["round"] + 1)
        self.assertEqual(len(self.build_calls), 3)
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.tool_calls, ["segmentation", "geometry"])
        self.assertEqual(self.review_calls[1].previous_review, first["previous_review"])
        self.assertIn(first["previous_review"], self.build_calls[2]["prompt"])
        self.assertIn(first["builder_diagnostic_path"], self.build_calls[2]["prompt"])

    def test_incomplete_or_unresolved_report_still_goes_to_independent_review(self):
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R001", "category": "inventory"}], "resolved_issue_ids": []}]
        for mode in ("incomplete", "unresolved"):
            with self.subTest(mode=mode):
                self.report_modes = [mode]
                before = len(self.review_calls)
                status = self.execute()
                self.assertEqual(len(self.review_calls), before + 1)
                self.assertFalse(status["accepted"])
                self.assertIsNone(status.get("builder_diagnostic_path"))

    def test_changed_initial_evidence_stays_fatal_even_when_report_is_missing(self):
        complete = self.complete_builder
        source = self.project / "observations/train/000001.png"
        original_source = source.read_bytes()
        for kind in ("source", "plan", "segments", "geometry"):
            with self.subTest(kind=kind):
                self.report_modes = ["missing"]

                def corrupt_after_build():
                    complete()
                    status = MODULE.read_json(self.project / "object_pipeline/status.json")
                    if kind == "source":
                        source.write_bytes(b"changed source")
                    elif kind == "geometry":
                        geometry = MODULE.read_json(self.project / status["geometry_path"])
                        (self.project / geometry["objects"][0]["mesh_path"]).write_text("changed mesh")
                    else:
                        path = self.project / status[f"{kind}_path"]
                        path.write_text(path.read_text() + "\n")

                with patch.object(self, "complete_builder", side_effect=corrupt_after_build):
                    with self.assertRaises(ValueError):
                        self.execute(max_rounds=2)
                status = MODULE.read_json(self.project / "object_pipeline/status.json")
                self.assertEqual(status["stage"], "failed")
                self.assertNotIn("builder_diagnostic_path", status)
                self.assertFalse(self.review_calls)
                source.write_bytes(original_source)

    def test_missing_masks_and_geometry_become_builder_tasks_without_reinitialization(self):
        self.missing = True
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R001", "category": "geometry"}], "resolved_issue_ids": []}]
        status = self.execute()
        self.assertFalse(status["accepted"])
        self.assertEqual(status["stage"], "needs_revision")
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.tool_calls, ["segmentation", "geometry"])
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(len(self.review_calls), 1)
        plan = MODULE.read_json(self.project / status["plan_path"])
        self.assertEqual(plan["objects"][0]["id"], "chair")
        self.assertEqual(plan["objects"][0]["geometry_strategy"], "fuse")
        _, _, _, tasks = MODULE.load_evidence(self.project, status)
        self.assertTrue(tasks)
        self.assertTrue(all(row["object_id"] == "chair" for row in tasks))
        self.assertFalse(any(row["blocking"] for row in tasks))

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
                self.assertIsNone(first.get("geometry_path"))
                original_plan_hash = MODULE.file_hash(self.project / first["plan_path"])
                original_segments_hash = MODULE.file_hash(self.project / first["segments_path"])
                plan_count = len(self.plan_calls)
                second = self.execute(resume=first["run_id"], stop_after="geometry")
                self.assertEqual(second["stage"], "ready_for_modeling")
                self.assertFalse(second["accepted"])
                self.assertEqual(len(self.plan_calls), plan_count)
                self.assertEqual(self.tool_calls[previous_tools:], ["segmentation", "geometry", "geometry"])
                self.assertEqual(second["plan_path"], first["plan_path"])
                self.assertEqual(second["segments_path"], first["segments_path"])
                self.assertEqual(MODULE.file_hash(self.project / second["plan_path"]), original_plan_hash)
                self.assertEqual(MODULE.file_hash(self.project / second["segments_path"]), original_segments_hash)
                self.assertGreater(second["round"], first["round"])
                self.assertIn(f"round_{second['round']:02d}", Path(second["geometry_path"]).parts)
                self.assertNotIn(f"round_{first['round']:02d}", Path(second["geometry_path"]).parts)
                self.assertFalse(self.build_calls)
                self.assertFalse(self.review_calls)

    def test_resume_after_planning_completes_only_missing_initialization_stages(self):
        first = self.execute(stop_after="plan")
        self.assertEqual(first["stage"], "planned")
        self.assertEqual(len(self.plan_calls), 1)
        self.assertFalse(self.tool_calls)
        plan_hash = MODULE.file_hash(self.project / first["plan_path"])
        second = self.execute(resume=first["run_id"], stop_after="geometry")
        self.assertEqual(second["stage"], "ready_for_modeling")
        self.assertFalse(second["accepted"])
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.tool_calls, ["segmentation", "geometry"])
        self.assertEqual(second["plan_path"], first["plan_path"])
        self.assertEqual(MODULE.file_hash(self.project / second["plan_path"]), plan_hash)
        self.assertFalse(self.build_calls)
        self.assertFalse(self.review_calls)

    def test_every_review_category_returns_to_builder_without_reinitialization(self):
        for category in ("inventory", "segmentation", "geometry", "appearance", "editability"):
            with self.subTest(category=category):
                self.plan_calls.clear()
                self.tool_calls.clear()
                self.build_calls.clear()
                self.review_calls.clear()
                self.reviews = [{"verdict": "revise", "issues": [{"id": "R001", "category": category}], "resolved_issue_ids": []},
                                {"verdict": "accepted", "issues": [], "resolved_issue_ids": ["R001"]}]
                status = self.execute(max_rounds=2)
                self.assertTrue(status["accepted"])
                self.assertEqual(len(self.plan_calls), 1)
                self.assertEqual(self.tool_calls, ["segmentation", "geometry"])
                self.assertEqual(len(self.build_calls), 2)
                self.assertEqual(self.review_calls[0].plan, self.review_calls[1].plan)
                self.assertEqual(self.review_calls[0].segments, self.review_calls[1].segments)
                self.assertEqual(self.review_calls[0].geometry, self.review_calls[1].geometry)
                self.assertEqual(self.review_calls[1].previous_review, "reconstruction/mock-review-1/round_01/review.json")
                self.assertIn(self.review_calls[1].previous_review, self.build_calls[1]["prompt"])

    def test_resume_carries_previous_review_without_replanning(self):
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R017", "category": "appearance"}], "resolved_issue_ids": []},
                        {"verdict": "accepted", "issues": [], "resolved_issue_ids": ["R017"]}]
        first = self.execute()
        self.assertEqual(first["stage"], "needs_revision")
        second = self.execute(resume=first["run_id"])
        self.assertTrue(second["accepted"])
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.review_calls[1].previous_review, first["previous_review"])
        self.assertIn(first["previous_review"], self.build_calls[1]["prompt"])

    def test_resume_preserves_missing_object_tasks_and_continues_builder(self):
        self.missing = True
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R017", "category": "segmentation"}], "resolved_issue_ids": []},
                        {"verdict": "accepted", "issues": [], "resolved_issue_ids": ["R017"]}]
        first = self.execute()
        self.assertEqual(first["stage"], "needs_revision")
        self.assertEqual(len(self.build_calls), 1)
        second = self.execute(resume=first["run_id"])
        self.assertTrue(second["accepted"])
        self.assertEqual(len(self.build_calls), 2)
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(self.tool_calls, ["segmentation", "geometry"])
        self.assertEqual(second["segments_path"], first["segments_path"])

    def test_unexplained_regions_are_modeling_tasks_not_a_planner_loop(self):
        self.unexplained = [{"frame_index": 1, "box": [.1, .1, .2, .2], "description": "small object not yet identified"}]
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R004", "category": "inventory"}], "resolved_issue_ids": []}]
        status = self.execute()
        self.assertFalse(status["accepted"])
        self.assertEqual(len(self.plan_calls), 1)
        self.assertEqual(len(self.build_calls), 1)
        _, _, _, tasks = MODULE.load_evidence(self.project, status)
        self.assertTrue(any(row["object_id"] == "scene" for row in tasks))
        self.assertFalse(any(row["blocking"] for row in tasks))

    def test_typed_geometry_limit_is_a_builder_task_but_unknown_failure_stops(self):
        self.geometry_status = "needs_revision"
        self.reviews = [{"verdict": "revise", "issues": [{"id": "R005", "category": "geometry"}], "resolved_issue_ids": []}]
        status = self.execute()
        self.assertFalse(status["accepted"])
        self.assertEqual(len(self.build_calls), 1)
        _, _, _, tasks = MODULE.load_evidence(self.project, status)
        self.assertTrue(any("face limit" in row["reason"] for row in tasks))
        self.assertFalse(any(row["blocking"] for row in tasks))

        def fatal_geometry(name, *args, **kwargs):
            if name == "geometry":
                raise RuntimeError("unclassified worker failure")
            return self.tool_worker(name, *args, **kwargs)

        with patch.object(MODULE, "run_tool", side_effect=fatal_geometry):
            with self.assertRaisesRegex(RuntimeError, "unclassified worker failure"):
                self.execute()
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(MODULE.read_json(self.project / "object_pipeline/status.json")["stage"], "failed")

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

    def test_one_review_then_feedback_model_stops_and_normal_resume_reviews_same_candidate(self):
        self.reviews = [{"verdict": "revise", "issues": [{"id": "shape", "category": "geometry"}], "resolved_issue_ids": []}]
        status = self.execute(max_rounds=3, stop_after="feedback-modeling")
        self.assertEqual(status["stage"], "modeled")
        self.assertFalse(status["accepted"])
        self.assertEqual((len(self.build_calls), len(self.review_calls), len(self.plan_calls)), (2, 1, 1))
        self.assertIn(status["previous_review"], self.build_calls[-1]["prompt"])
        again = self.execute(resume=status["run_id"], stop_after="feedback-modeling", max_rounds=3)
        self.assertEqual(again["stage"], "modeled")
        self.assertEqual((len(self.build_calls), len(self.review_calls)), (2, 1))
        self.execute(resume=status["run_id"])
        self.assertEqual((len(self.build_calls), len(self.review_calls)), (2, 2))

    def test_one_review_limit_survives_resume_before_feedback_modeling(self):
        self.reviews = [{"verdict": "blocked", "issues": [], "resolved_issue_ids": []}]
        first = self.execute(max_rounds=1, stop_after="feedback-modeling")
        self.assertEqual(len(self.review_calls), 1)
        second = self.execute(resume=first["run_id"], max_rounds=3, stop_after="feedback-modeling")
        self.assertEqual(second["stage"], "modeled")
        self.assertEqual((len(self.build_calls), len(self.review_calls)), (2, 1))

    def test_feedback_stop_keeps_even_a_passing_review_for_user_inspection(self):
        status = self.execute(max_rounds=2, stop_after="feedback-modeling")
        self.assertFalse(self.review_calls[0].publish_after_review)
        self.assertEqual(status["stage"], "modeled")
        self.assertFalse(status["accepted"])
        self.assertEqual((len(self.build_calls), len(self.review_calls)), (2, 1))

    def test_two_new_reviews_start_with_existing_candidate_and_stop_after_second_repair(self):
        for verdict in ("revise", "accepted", "blocked"):
            with self.subTest(verdict=verdict):
                self.reviews = [{"verdict": verdict, "issues": [], "resolved_issue_ids": []}]
                first = self.execute(max_rounds=2, stop_after="feedback-modeling")
                original = (self.project / "object_pipeline" / first["run_id"] / "status.json").read_bytes()
                before_reviews, before_plans, before_tools = len(self.review_calls), len(self.plan_calls), len(self.tool_calls)
                self.order.clear()
                result = self.execute(repair_from=first["run_id"], repair_cycles=2)
                self.assertEqual(self.order, ["review", "repair", "review", "repair"])
                self.assertEqual(self.review_calls[before_reviews].candidate, first["candidate_path"])
                self.assertEqual(self.review_calls[-1].previous_review, f"reconstruction/mock-review-{before_reviews + 1}/round_01/review.json")
                self.assertTrue(all(not call.publish_after_review for call in self.review_calls[before_reviews:]))
                self.assertEqual((len(self.plan_calls), len(self.tool_calls)), (before_plans, before_tools))
                self.assertEqual((result["reviews_completed"], result["repairs_completed"]), (2, 2))
                self.assertEqual(result["stage"], "modeled")
                self.assertFalse(result["accepted"])
                self.assertNotEqual(result["run_id"], first["run_id"])
                self.assertEqual((self.project / "object_pipeline" / first["run_id"] / "status.json").read_bytes(), original)
                self.execute(resume=result["run_id"])
                self.assertEqual(self.order, ["review", "repair", "review", "repair"])

    def test_repair_batch_resume_waits_for_existing_builder_and_preserves_budget(self):
        first = self.execute(max_rounds=2, stop_after="feedback-modeling")
        self.order.clear()
        self.interrupt_builder = True
        with self.assertRaises(KeyboardInterrupt):
            self.execute(repair_from=first["run_id"], repair_cycles=2)
        interrupted = MODULE.read_json(self.project / "object_pipeline/status.json")
        self.assertEqual(self.order, ["review", "repair"])
        result = self.execute(resume=interrupted["run_id"], repair_cycles=3)
        self.assertEqual(self.order, ["review", "repair", "review", "repair"])
        self.assertEqual((result["reviews_completed"], result["repairs_completed"], result["repair_limit"]), (2, 2, 2))

    def test_uncertain_review_is_not_automatically_repeated(self):
        first = self.execute(max_rounds=2, stop_after="feedback-modeling")
        self.order.clear()
        with patch.object(MODULE, "review_candidate", side_effect=RuntimeError("review interrupted")) as review:
            with self.assertRaisesRegex(RuntimeError, "review interrupted"):
                self.execute(repair_from=first["run_id"], repair_cycles=2)
            interrupted = MODULE.read_json(self.project / "object_pipeline/status.json")
            with self.assertRaisesRegex(RuntimeError, "review outcome is uncertain"):
                self.execute(resume=interrupted["run_id"])
            self.assertEqual(review.call_count, 1)
        self.assertEqual(self.order, [])

    def test_ultra_batch_can_start_from_completed_repair_after_review_failure(self):
        first = self.execute(max_rounds=2, stop_after="feedback-modeling")
        real_review = self.review_worker

        def fail_second(options):
            if len(self.review_calls) == 2:
                raise RuntimeError("review auth expired")
            return real_review(options)

        with patch.object(MODULE, "review_candidate", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "review auth expired"):
                self.execute(repair_from=first["run_id"], repair_cycles=2)
        failed = MODULE.read_json(self.project / "object_pipeline/status.json")
        self.assertEqual(failed["repairs_completed"], 1)
        self.effort = "ultra"
        self.order.clear()
        result = self.execute(repair_from=failed["run_id"], repair_cycles=2)
        self.assertEqual(self.order, ["review", "repair", "review", "repair"])
        self.assertEqual(result["builder"], {"model": "gpt-6-astra", "effort": "ultra"})
        self.assertEqual(self.review_calls[-2].candidate, failed["candidate_path"])

    def test_repair_from_rejects_unfinished_builder(self):
        self.interrupt_builder = True
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        source = MODULE.read_json(self.project / "object_pipeline/status.json")
        with self.assertRaisesRegex(ValueError, "stopped, unpublished modeled candidate"):
            self.execute(repair_from=source["run_id"], repair_cycles=2)

    def test_bad_feedback_report_does_not_dispatch_a_third_modeling_task(self):
        self.reviews = [{"verdict": "revise", "issues": [], "resolved_issue_ids": []}]
        self.report_modes = ["valid", "missing"]
        status = self.execute(max_rounds=3, stop_after="feedback-modeling")
        self.assertEqual(status["stage"], "needs_revision")
        self.assertTrue(status["feedback_modeling_finished"])
        self.execute(resume=status["run_id"], max_rounds=3, stop_after="feedback-modeling")
        self.assertEqual((len(self.build_calls), len(self.review_calls)), (2, 1))

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
