import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


DIRECTORY = Path(__file__).resolve().parents[2] / "experiments/reconstruction"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("experiment_monitor", DIRECTORY / "monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class ExperimentMonitorTests(unittest.TestCase):
    def test_pool_reserves_distinct_trials_and_respects_pre_gate_admission(self):
        state={"trials":[{"id":"s__single_basic__test__ultra", "scene":"s", "model":"test",
                           "condition":"single_basic", "effort":"ultra", "stage":"queued"},
                          {"id":"s__harness_tools__test__high", "scene":"s", "model":"test",
                           "condition":"harness_tools", "effort":"high", "stage":"queued"}], "issues":[]}
        settings={"workers_per_model":2,"before_gate_trials":[state["trials"][0]["id"]]}
        with patch.object(monitor,"worker_alive",return_value=False), \
             patch.object(monitor,"start_worker",return_value={}) as start:
            monitor.fill_worker_pool(self.root,self.root/"runtime.json",self.protocol,state,settings,True,False)
        self.assertEqual(start.call_count,1)
        state["parallelism"]={}
        with patch.object(monitor,"worker_alive",return_value=False), \
             patch.object(monitor,"start_worker",return_value={}) as start:
            monitor.fill_worker_pool(self.root,self.root/"runtime.json",self.protocol,state,settings,False,False)
        self.assertEqual(start.call_count,2)
        self.assertEqual(len(set(tuple(c.args[3]) for c in start.call_args_list)),2)

    def test_pool_adopts_existing_worker_and_does_not_duplicate_its_queued_trial(self):
        identifier="s__single_basic__test__ultra"
        args=["python","run.py","--root",str(self.root),"--model","test","--scene","s",
              "--condition","single_basic","--effort","ultra"]
        monitor.write(self.root/"test-coordinator.json",{"pid":1,"args":args})
        state={"trials":[{"id":identifier,"scene":"s","model":"test","condition":"single_basic",
                           "effort":"ultra","stage":"queued"}],"issues":[]}
        settings={"workers_per_model":2,"before_gate_trials":[identifier]}
        with patch.object(monitor,"worker_alive",side_effect=lambda p,*_:p.name=="test-coordinator.json"), \
             patch.object(monitor,"start_worker") as start:
            monitor.fill_worker_pool(self.root,self.root/"runtime.json",self.protocol,state,settings,True,False)
        start.assert_not_called()
        self.assertEqual(state["parallelism"]["assigned_trials"],[identifier])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.protocol = {"scenes": {"s": "id"}, "models": ["test"], "conditions": ["single_basic"],
                         "main_effort": "ultra", "ablation_effort": "high", "modeling_passes": 3}
        monitor.write(self.root / "protocol.json", self.protocol)

    def trial(self, condition, effort, stage):
        trial = self.root / "runs" / f"s__{condition}__test__{effort}"
        monitor.write(trial / "status.json", {"stage": stage, "error": "preserved" if stage == "failed" else None})
        return trial

    def test_orphaned_call_is_not_silently_skipped_or_retried(self):
        trial = self.trial("single_basic", "ultra", "modeling")
        original = (trial / "status.json").read_bytes()
        with patch.object(monitor, "worker_alive", return_value=False), \
             patch.object(monitor, "start_worker", return_value={}) as start:
            state = monitor.tick(self.root, self.root / "runtime.json")
        self.assertIn("orphaned_controller", {i["kind"] for i in state["issues"]})
        self.assertFalse(any(call.args[2] == "run.py" for call in start.call_args_list))
        self.assertEqual((trial / "status.json").read_bytes(), original)

    def test_only_queued_trials_restart_after_controller_exit(self):
        failed = self.trial("single_basic", "ultra", "failed")
        original = (failed / "status.json").read_bytes()
        with patch.object(monitor, "worker_alive", return_value=False), \
             patch.object(monitor, "start_worker", return_value={}) as start:
            monitor.tick(self.root, self.root / "runtime.json")
        self.assertEqual(sum(call.args[2] == "run.py" for call in start.call_args_list), 1)
        self.assertEqual((failed / "status.json").read_bytes(), original)

    def test_finished_modeling_waits_for_all_frozen_evaluations(self):
        for condition, effort in (("single_basic", "ultra"), ("harness_tools", "high")):
            trial = self.trial(condition, effort, "modeled")
            for i in range(3):
                monitor.write(trial / "checkpoints" / f"M{i}" / "frozen.json", {})
                if i < 2:
                    monitor.write(self.root / "evaluation_results" / trial.name / f"M{i}" / "complete.json", {})
        self.assertFalse(monitor.snapshot(self.root)["automated_finished"])
        for trial in (self.root / "runs").glob("*"):
            monitor.write(self.root / "evaluation_results" / trial.name / "M2/error.json", {"error": "fixture"})
        state = monitor.snapshot(self.root)
        self.assertTrue(state["automated_finished"])
        self.assertEqual(state["outcome"], "finished_with_errors")
        self.assertEqual(state["semantic_edit_verification"], "pending_independent_visual_check")

    def test_unfinished_workers_are_not_duplicated(self):
        with patch.object(monitor, "worker_alive", return_value=True), \
             patch.object(monitor, "start_worker") as start:
            monitor.tick(self.root, self.root / "runtime.json")
        start.assert_not_called()

    def test_consecutive_failures_hold_new_launches(self):
        self.protocol["halt_after_consecutive_failures"] = 2
        monitor.write(self.root / "protocol.json", self.protocol)
        self.trial("single_basic", "ultra", "failed")
        self.trial("harness_tools", "high", "failed")
        with patch.object(monitor, "worker_alive", return_value=True):
            result = monitor.tick(self.root, self.root / "runtime.json")
        self.assertTrue((self.root / "launch_hold.json").exists())
        self.assertEqual(result["outcome"], "needs_attention")

    def test_full_chain_gate_releases_only_after_all_three_evaluations(self):
        identifier = "s__harness_tools__test__high"
        monitor.write(self.root / "execution_gate.json", {"trial": identifier})
        with patch.object(monitor, "worker_alive", return_value=False), \
             patch.object(monitor, "start_worker", return_value={}) as start:
            monitor.tick(self.root, self.root / "runtime.json")
        run = next(c for c in start.call_args_list if c.args[2] == "run.py")
        self.assertIn("--scene", run.args[3])
        self.assertIn("high", run.args[3])
        trial = self.trial("harness_tools", "high", "modeled")
        for i in range(3):
            monitor.write(trial / "checkpoints" / f"M{i}" / "frozen.json", {})
            monitor.write(self.root / "evaluation_results" / trial.name / f"M{i}" / "complete.json", {})
        with patch.object(monitor, "worker_alive", return_value=False), \
             patch.object(monitor, "start_worker", return_value={}) as start:
            monitor.tick(self.root, self.root / "runtime.json")
        self.assertIn("passed_at", monitor.read(self.root / "execution_gate.json"))
        run = next(c for c in start.call_args_list if c.args[2] == "run.py")
        self.assertNotIn("--scene", run.args[3])


if __name__ == "__main__":
    unittest.main()
