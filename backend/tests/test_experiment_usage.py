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
    def test_all_input_images_receive_their_camera(self):
        poses = [[[i, 0, 0, 0]] for i in range(54)]
        data = dict(w=640, h=480, fl_x=500, fl_y=510, cx=320, cy=240,
                    frames=[dict(source_index=100+i, transform_matrix=p) for i, p in enumerate(poses)])
        manifest = common.camera_manifest(data, 48)
        self.assertEqual(manifest["intrinsics"]["fy"], 510)
        self.assertEqual(sum(f["role"] == "train" for f in manifest["frames"]), 48)
        self.assertEqual(sum(f["role"] == "heldout" for f in manifest["frames"]), 6)
        for i, frame in enumerate(manifest["frames"]):
            self.assertEqual(frame["camera_to_world"], poses[i])
            self.assertEqual(frame["source_index"], 100+i)
            self.assertEqual(frame["image"], f"observations/{frame['role']}/{i:06d}.png")

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
