"""Instance selection, missing detections and source isolation without model weights."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/sam3_segment.py"
SPEC = importlib.util.spec_from_file_location("sam3_segment", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Sam3SelectionTests(unittest.TestCase):
    def test_box_identity_takes_precedence_over_detector_score(self):
        masks = np.zeros((2, 100, 200), dtype=bool)
        masks[0, 20:80, 10:70] = True
        masks[1, 20:80, 120:180] = True
        index, _ = MODULE.select_mask(masks, [.99, .6], [.6, .2, .9, .8])
        self.assertEqual(index, 1)

    def test_ambiguous_or_background_masks_are_not_assigned(self):
        masks = np.zeros((2, 100, 100), dtype=bool)
        masks[:, 20:80, 20:80] = True
        index, reason = MODULE.select_mask(masks, [.9, .8], [.2, .2, .8, .8])
        self.assertIsNone(index)
        self.assertIn("multiple", reason)
        index, _ = MODULE.select_mask(np.ones((1, 100, 100), dtype=bool), [.99], [.2, .2, .4, .4])
        self.assertIsNone(index)

    def test_no_detection_returns_missing_instead_of_an_empty_valid_mask(self):
        class Tensor:
            def __init__(self, array): self.array = array
            def squeeze(self, axis): return Tensor(self.array.squeeze(axis))
            def cpu(self): return self
            def float(self): return self
            def numpy(self): return self.array

        class Processor:
            def __init__(self): self.model = self
            def reset_all_prompts(self, state): pass
            def set_text_prompt(self, **kwargs):
                return {"masks": Tensor(np.zeros((0, 1, 100, 200), dtype=bool)),
                        "scores": Tensor(np.zeros(0))}
            def predict_inst(self, state, *, box, **kwargs):
                np.testing.assert_allclose(box, [40, 20, 160, 80], atol=.001)
                return np.zeros((1, 100, 200), dtype=bool), np.array([.95]), None

        result = MODULE.segment_observation(Processor(), {"original_width": 200, "original_height": 100},
            {"segmentation_prompt": "lamp"}, {"box": [.2, .2, .8, .8]}, .35)
        self.assertIsNone(result[0])
        self.assertEqual([attempt["method"] for attempt in result[3]], ["text", "box"])

    def test_model_runtime_failure_is_not_reported_as_missing_object(self):
        class Broken:
            def reset_all_prompts(self, state): raise RuntimeError("GPU unavailable")
        with self.assertRaisesRegex(RuntimeError, "GPU unavailable"):
            MODULE.segment_observation(Broken(), {}, {}, {}, .35)

    def test_training_identity_and_image_bytes_are_bound_to_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "observations/train/000001.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"original bytes")
            manifest = root / "observations/manifest.json"
            manifest.write_text(json.dumps({"train": [{"index": 1, "path": "observations/train/000001.png",
                                                       "sha256": MODULE.digest(image)}]}))
            plan = {"source_manifest_sha256": MODULE.digest(manifest),
                    "objects": [{"id": "chair", "observations": [{"frame_index": 1, "box": [.1, .1, .9, .9]}]}]}
            path = root / "plan.json"
            path.write_text(json.dumps(plan))
            MODULE.read_inputs(root, path)
            image.write_bytes(b"changed bytes")
            with self.assertRaisesRegex(ValueError, "Prepared image changed"):
                MODULE.read_inputs(root, path)
            plan["objects"][0]["observations"][0]["frame_index"] = 0
            path.write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "training frames"):
                MODULE.read_inputs(root, path)

    def test_artifact_paths_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside").symlink_to("/tmp")
            for path in ("../mask.png", "/tmp/mask.png", "outside/mask.png"):
                with self.assertRaises(ValueError):
                    MODULE.project_file(root, path)


if __name__ == "__main__":
    unittest.main()
