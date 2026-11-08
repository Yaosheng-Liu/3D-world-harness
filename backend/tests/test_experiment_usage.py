import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


PATH = Path(__file__).resolve().parents[2] / "experiments/reconstruction/common.py"
SPEC = importlib.util.spec_from_file_location("experiment_common", PATH)
common = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(common)


class ExperimentUsageTests(unittest.TestCase):
    def test_resume_snapshots_are_not_added(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.jsonl"
            rows=[{"type":"thread.started","thread_id":"t"},
                  {"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20,
                    "cached_input_tokens":80,"reasoning_output_tokens":5}},
                  {"type":"turn.completed","usage":{"input_tokens":150,"output_tokens":30,
                    "cached_input_tokens":120,"reasoning_output_tokens":8}}]
            path.write_text("\n".join(json.dumps(r) for r in rows))
            data=common.cli_usage(path)
            self.assertEqual(data["usage"]["total_tokens"],180)
            self.assertEqual(data["usage"]["cached_input_tokens"],120)

    def test_failed_turn_does_not_become_zero_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"events.jsonl"
            path.write_text('{"type":"turn.failed","error":"network"}\n')
            self.assertIsNone(common.cli_usage(path)["usage"]["total_tokens"])

    def test_experiment_matrix_has_no_duplicate_ultra_runs(self):
        protocol=common.read(PATH.with_name("protocol.json"))
        rows=common.matrix(protocol)
        self.assertEqual(len(rows),8)
        self.assertEqual(len({tuple(sorted(r.items())) for r in rows}),8)
        self.assertEqual(sum(r["effort"]=="high" for r in rows),2)


if __name__=="__main__":
    unittest.main()
