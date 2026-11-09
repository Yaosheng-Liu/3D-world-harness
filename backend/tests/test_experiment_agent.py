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
