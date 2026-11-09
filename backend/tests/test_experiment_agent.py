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
                 patch.object(runner, "build_world", return_value={}), \
                 patch.object(runner, "freeze_checkpoint", return_value=({"objects":[{"id":"a"}]}, {})):
                runner.run_trial(root, spec, {}, protocol)
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


if __name__ == "__main__":
    unittest.main()
