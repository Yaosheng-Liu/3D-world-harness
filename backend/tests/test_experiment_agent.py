import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


DIRECTORY = Path(__file__).resolve().parents[2] / "experiments/reconstruction"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("experiment_runner", DIRECTORY / "run.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ExperimentAgentTests(unittest.TestCase):
    def test_planner_resume_archives_logs_and_keeps_schema_and_read_only_scope(self):
        core = sys.modules["run_reconstruction"]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            runner.write(project/"observations/manifest.json", {})
            output = project/"evidence/plan"
            output.mkdir(parents=True)
            (output/"events.jsonl").write_text("original timeout log\n")
            def execute(command, **kwargs):
                runner.write(output/"plan.json", {"summary": "continued"})
                kwargs["stdout"].write(json.dumps({"type": "thread.started", "thread_id": "old-thread"})+"\n")
            with patch.object(core.subprocess, "run", side_effect=execute) as call, \
                 patch.object(core, "validate_plan", side_effect=lambda p, plan: plan), \
                 patch.object(core, "observations"), patch.object(core, "contact_sheets") as sheets:
                core.run_planner(project, output, {"planner": {"codex": "codex", "model": "test", "effort": "ultra"},
                                 "planner_timeout": None, "planner_resume_thread": "old-thread"})
            command = call.call_args.args[0]
            self.assertEqual(command[-3:], ["resume", "old-thread", "-"])
            self.assertEqual(command[command.index("-s")+1], "read-only")
            self.assertIn("--output-schema", command)
            self.assertIsNone(call.call_args.kwargs["timeout"])
            sheets.assert_not_called()
            self.assertEqual(next((output/"attempts").glob("*/events.jsonl")).read_text(), "original timeout log\n")

    def test_planner_resume_keeps_cumulative_tokens_and_accumulates_elapsed_time(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)/"run"
            project = trial/"project"
            runner.write(trial/"resume.json", {"planner_thread": "old-thread"})
            runner.write(trial/"planner_usage.json", {"seconds": 900, "usage": {"total_tokens": 100}})
            def execute(project, output, runtime):
                output.mkdir(parents=True)
                events = [{"type": "thread.started", "thread_id": "old-thread"},
                          {"type": "turn.completed", "usage": {"input_tokens": 160, "output_tokens": 20}}]
                (output/"events.jsonl").write_text("\n".join(json.dumps(e) for e in events)+"\n")
                return "evidence/plan/plan.json", {}
            with patch.object(runner, "run_planner", side_effect=execute) as planner, \
                 patch.object(runner, "role_home", return_value=trial/"home"), \
                 patch.object(runner, "initialize_objects"):
                runner.planning(project, trial, {"codex": "codex", "proxy": "", "planner_timeout_seconds": None},
                                {"planner": {"model": "test", "effort": "ultra"}, "planner_timeout_seconds": 900})
            options = planner.call_args.args[2]
            self.assertIsNone(options["planner_timeout"])
            self.assertEqual(options["planner_resume_thread"], "old-thread")
            usage = runner.read(trial/"planner_usage.json")
            self.assertEqual(usage["usage"]["total_tokens"], 180)
            self.assertGreaterEqual(usage["seconds"], 900)

    def test_linked_continuation_verifies_its_existing_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, original = base/"batch", base/"original"
            spec = {"scene": "s", "condition": "single_basic", "model": "test", "effort": "high"}
            identifier = "s__single_basic__test__high"
            (root/"runs").mkdir(parents=True)
            (original/"project").mkdir(parents=True)
            (root/"runs"/identifier).symlink_to(original, target_is_directory=True)
            runner.write(original/"resume.json", {})
            runner.write(original/"status.json", {"started_at": 0, "checkpoints": [
                {"candidate": "models/M0/candidate.json", "receipt": "models/M0/build_receipt.json"}]})
            note = original/"checkpoints/M0/note.txt"
            note.parent.mkdir(parents=True)
            note.write_text("frozen evidence")
            runner.write(note.parent/"frozen.json", {"files": {"note.txt": runner.digest(note)}})
            runner.run_trial(root, spec, {}, {"modeling_passes": 1})
            self.assertEqual(runner.read(original/"status.json")["stage"], "modeled")

    def test_initialization_recovery_keeps_completed_segmentation(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            runner.write(project/"evidence/segments/segments.json", {"existing": True})
            with patch.object(runner.subprocess, "run") as execute:
                runner.initialize_objects(project, {"tool_timeout_seconds": None}, "evidence/plan/plan.json")
            self.assertEqual(execute.call_count, 1)
            self.assertIn("geometry", execute.call_args.args[0])
            self.assertNotIn("segment", execute.call_args.args[0])
            self.assertIsNone(execute.call_args.kwargs["timeout"])

    def test_failed_plan_validation_preserves_usage_and_original_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)/"project"
            trial = Path(directory)/"trial"
            def fail(project, output, runtime):
                output.mkdir(parents=True)
                events = [{"type":"thread.started","thread_id":"planner-thread"},
                          {"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}]
                (output/"events.jsonl").write_text("\n".join(json.dumps(e) for e in events)+"\n")
                raise ValueError("invalid planner view")
            with patch.object(runner,"role_home",return_value=trial/"home"), \
                 patch.object(runner,"run_planner",side_effect=fail):
                with self.assertRaisesRegex(ValueError,"invalid planner view"):
                    runner.planning(project,trial,{"codex":"codex","proxy":""},
                                    {"planner":{"model":"test","effort":"xhigh"},"planner_timeout_seconds":900})
            record=runner.read(trial/"planner_usage.json")
            self.assertEqual(record["usage"]["total_tokens"],12)
            self.assertEqual(record["error"],"invalid planner view")

    def test_interrupted_review_reuses_frozen_candidate_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            project, trial = Path(directory)/"project", Path(directory)/"trial"
            scene={"objects":[],"provenance":{"sources":[]}}
            runner.write(project/"models/M1/candidate.json", scene)
            runner.write(project/"reviews/R2/candidate.json", {"scene_hash":runner.scene_hash(scene),"files":{}})
            runner.write(project/"reviews/R2/inspection.json", {"inspection_paths":[],"depth":{"inspection_paths":[]}})
            runner.write(project/"reviews/R2/editability.json", {})
            runner.write(trial/"calls/review_R2/usage.json", {"error":"original timeout"})
            def review(*args,**kwargs):
                runner.write(args[1]/"response.json", {"candidate_id":"same"})
            with patch.object(runner,"bind_candidate_sources",return_value=scene), \
                 patch.object(runner,"verify_files"), patch.object(runner,"validate_review"), \
                 patch.object(runner,"review_schema",return_value={}), patch.object(runner,"review_prompt",return_value="review"), \
                 patch.object(runner,"role_home",return_value=trial/"home"), \
                 patch.object(runner,"agent",side_effect=review) as agent, \
                 patch.object(runner,"build_world") as build, patch.object(runner,"freeze_candidate") as freeze:
                path=runner.feedback(project,trial,{}, {"reviewer":{},"review_timeout_seconds":None},
                                     1,"models/M1/candidate.json","receipt", "plan", None)
            self.assertEqual(path,"reviews/R2/review.json")
            self.assertIsNone(agent.call_args.kwargs["timeout"])
            self.assertEqual(agent.call_args.args[1].name,"review_R2_attempt2")
            build.assert_not_called();freeze.assert_not_called()
            self.assertEqual(runner.read(trial/"calls/review_R2/usage.json")["error"],"original timeout")

    def test_valid_existing_review_is_reused_without_another_reviewer_call(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            runner.write(project/"reviews/R1/review.json", {"candidate_id":"existing"})
            runner.write(project/"reviews/R1/candidate.json", {"files":{}})
            with patch.object(runner, "verify_files"), patch.object(runner, "validate_review"), \
                 patch.object(runner, "agent") as reviewer, patch.object(runner, "freeze_candidate") as freeze:
                result = runner.feedback(project, project, {}, {}, 0, "candidate", "receipt", "plan", None)
            self.assertEqual(result, "reviews/R1/review.json")
            reviewer.assert_not_called()
            freeze.assert_not_called()

    def test_single_agent_feedback_generates_no_audit_material(self):
        with patch.object(runner, "freeze_candidate") as freeze, \
             patch.object(runner, "build_world") as build, patch.object(runner, "agent") as reviewer:
            result = runner.feedback(Path("unused"), Path("unused"), {}, {}, 0, "candidate", "receipt", None, None)
        self.assertIsNone(result)
        freeze.assert_not_called()
        build.assert_not_called()
        reviewer.assert_not_called()

    def test_seeded_single_agent_keeps_M0_and_starts_clean_repair_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {"scene":"s", "model":"test", "condition":"single_basic", "effort":"high"}
            trial = root/"runs/s__single_basic__test__high"
            initial = {"name":"M0", "candidate":"models/M0/candidate.json", "receipt":"models/M0/build_receipt.json"}
            runner.write(trial/"resume.json", {"thread_id":None, "raw_cumulative_usage":None, "plan":None})
            runner.write(trial/"status.json", {"stage":"queued", "started_at":0, "checkpoints":[initial]})
            runner.write(trial/"input_hashes.json", {})
            runner.write(trial/"checkpoints/M0/frozen.json", {"files":{}})
            working = root/"working"
            working.mkdir()
            (trial/"project").symlink_to(working, target_is_directory=True)
            usage = {"thread_id":"new-repair", "raw_cumulative_usage":{"input_tokens":10}, "usage":{"input_tokens":10}}
            protocol = {"modeling_passes":3, "modeling_timeout_seconds":[3600,None,None],
                        "input_prompt":"Build", "repair_prompt":"Revise"}
            with patch.object(runner, "agent", return_value=usage) as agent, \
                 patch.object(runner, "initialize_run") as initialize, \
                 patch.object(runner, "role_home", return_value=root/"home"), \
                 patch.object(runner, "verify_files"), patch.object(runner, "validate_fixed_views"), \
                 patch.object(runner, "bind_candidate_sources", return_value={"objects":[{"id":"a"}]}), \
                 patch.object(runner, "build_world", return_value={}) as build, \
                 patch.object(runner, "freeze_checkpoint", return_value=({"objects":[{"id":"a"}]}, {})):
                runner.run_trial(root, spec, {"blender":"/custom/blender", "build_timeout_seconds":None}, protocol)
            self.assertTrue(all(c.kwargs["blender"] == "/custom/blender" for c in build.call_args_list))
            self.assertTrue(all(c.kwargs["timeout"] is None for c in build.call_args_list))
            initialize.assert_not_called()
            self.assertEqual(agent.call_count, 2)
            self.assertEqual(agent.call_args_list[0].args[0], working)
            self.assertIsNone(agent.call_args_list[0].kwargs["thread"])
            self.assertEqual(agent.call_args_list[1].kwargs["thread"], "new-repair")
            self.assertTrue(all(c.kwargs["timeout"] is None for c in agent.call_args_list))
            self.assertTrue(all("self_inspection" not in c.args[5] for c in agent.call_args_list))
            state = runner.read(trial/"status.json")
            self.assertEqual(state["stage"], "modeled")
            self.assertEqual(state["checkpoints"][0], initial)
            self.assertEqual([c["name"] for c in state["checkpoints"]], ["M0","M1","M2"])

    def test_unlimited_repair_has_no_subprocess_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            process = MagicMock(pid=123, returncode=0)
            events = [{"type":"thread.started", "thread_id":"test-thread"},
                      {"type":"turn.completed", "usage":{"input_tokens":10, "output_tokens":2,
                                                            "cached_input_tokens":0}}]
            def launch(argv, **kwargs):
                kwargs["stdout"].write("\n".join(json.dumps(row) for row in events)+"\n")
                return process
            with patch.object(runner.subprocess, "Popen", side_effect=launch), \
                 patch.object(runner.subprocess, "run") as stop:
                result = runner.agent(project, project/"call", {"codex":"codex", "proxy":"local"},
                                      {"model":"test", "effort":"high"}, project/"home", "Repair", timeout=None)
            self.assertIsNone(process.communicate.call_args.kwargs["timeout"])
            stop.assert_not_called()
            self.assertEqual(result["completed_turns"], 1)
            self.assertEqual(result["usage"]["total_tokens"], 12)

    def test_saved_submission_continues_to_review_without_another_modeling_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {"scene":"s", "model":"test", "condition":"harness_tools", "effort":"high"}
            trial = root/"runs/s__harness_tools__test__high"
            project = trial/"project"
            initial = {"name":"M0", "candidate":"models/M0/candidate.json", "receipt":"models/M0/build_receipt.json"}
            runner.write(trial/"status.json", {"stage":"queued", "started_at":0, "checkpoints":[initial]})
            runner.write(trial/"input_hashes.json", {})
            runner.write(trial/"checkpoints/M0/frozen.json", {"files":{}})
            runner.write(project/"models/M1/candidate.json", {"saved":"M1"})
            saved = {"thread_id":"saved-thread", "raw_cumulative_usage":{"input_tokens":20},
                     "usage":{"input_tokens":10}, "error":None, "exit_code":0, "completed_turns":1}
            runner.write(trial/"calls/model_M1/usage.json", saved)
            runner.write(trial/"resume.json", {"plan":"plan.json", "thread_id":"saved-thread",
                "raw_cumulative_usage":{"input_tokens":20}, "saved_submission":{
                    "checkpoint":1, "call":"calls/model_M1",
                    "candidate_sha256":runner.digest(project/"models/M1/candidate.json")}})
            protocol = {"modeling_passes":3, "modeling_timeout_seconds":[3600,None,None],
                        "input_prompt":"Build", "repair_prompt":"Revise"}
            with patch.object(runner,"agent",return_value=saved) as agent, \
                 patch.object(runner,"role_home",return_value=trial/"home"), \
                 patch.object(runner,"feedback",return_value="reviews/review.json") as feedback, \
                 patch.object(runner,"validate_fixed_views"), \
                 patch.object(runner,"bind_candidate_sources",return_value={"objects":[{"id":"a"}]}), \
                 patch.object(runner,"build_world",return_value={}), \
                 patch.object(runner,"freeze_checkpoint",return_value=({"objects":[{"id":"a"}]},{})):
                runner.run_trial(root,spec,{"blender":"/custom/blender"},protocol)
            self.assertEqual(agent.call_count,1)
            self.assertEqual(agent.call_args.args[1].name,"model_M2")
            self.assertEqual(agent.call_args.kwargs["previous_usage"],saved["raw_cumulative_usage"])
            self.assertEqual(feedback.call_count,2)
            self.assertEqual(runner.read(trial/"status.json")["stage"],"modeled")


if __name__ == "__main__":
    unittest.main()
