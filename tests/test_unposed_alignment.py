"""Alignment invariants: recovered gauge, right-handedness, and invalid trajectories."""
import sys
from pathlib import Path
import unittest
import tempfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/reconstruction"))
from unposed import fit_similarity, transform_cameras, initialize_run
from common import read, write


class UnposedAlignmentTest(unittest.TestCase):
    def test_recovers_one_global_gauge_for_cameras_and_points(self):
        centers = np.array([[0., 0, 0], [1, 0, 0], [0, 2, 0], [1, 1, 3]])
        rotation = np.array([[0., -1, 0], [1, 0, 0], [0, 0, 1]])
        translation = np.array([3., -4, 5])
        estimated = 2.5*(centers @ rotation.T)+translation
        fit = fit_similarity(centers, estimated)
        self.assertAlmostEqual(fit["scale"], 2.5)
        np.testing.assert_allclose(fit["rotation"], rotation, atol=1e-12)
        np.testing.assert_allclose(fit["translation"], translation)
        cameras = np.repeat(np.eye(4)[None], 4, axis=0)
        cameras[:, :3, 3] = centers
        result = transform_cameras(cameras, fit)
        np.testing.assert_allclose(result[:, :3, 3], estimated)
        np.testing.assert_allclose(result[:, :3, :3], np.broadcast_to(rotation, (4, 3, 3)), atol=1e-12)

    def test_rgb_only_packet_contains_no_calibration_or_scaffold(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared/room/rgb_project"
            write(shared / "observations/manifest.json", {"train": [], "heldout": []})
            (shared / "training_contact_sheet.jpg").write_bytes(b"test image")
            write(root / "protocol.json", {"edit_targets": {"room": ["chair"]}})
            run = root / "runs/trial"
            run.mkdir(parents=True)
            def forbidden(*args):
                self.fail("RGB-only packet invoked calibrated initialization")
            spec = {"input_mode": "unposed_rgb", "condition": "single_basic", "scene": "room"}
            project = initialize_run(root, run, spec, {"blender": "/test/blender"}, forbidden)
            self.assertFalse((project / "cameras.json").exists())
            self.assertFalse((project / "world/scene.json").exists())
            self.assertFalse((project / "dense").exists())
            self.assertEqual(read(project / "observations/manifest.json"), {"train": [], "heldout": []})
            self.assertIn("CAMERA_OUTPUT.md", read(run / "input_hashes.json"))

    def test_reflection_is_not_fitted_away(self):
        centers = np.array([[0., 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3]])
        mirrored = centers*np.array([-1, 1, 1])
        fit = fit_similarity(centers, mirrored)
        self.assertAlmostEqual(np.linalg.det(fit["rotation"]), 1)
        self.assertGreater(fit["camera_center_rmse_in_estimated_units"], .1)

    def test_collinear_or_missing_trajectories_cannot_set_the_gauge(self):
        line = np.array([[0., 0, 0], [1, 0, 0], [2, 0, 0]])
        with self.assertRaisesRegex(ValueError, "degenerate"):
            fit_similarity(line, line)
        with self.assertRaisesRegex(ValueError, "three"):
            fit_similarity(line[:2], line[:2])


if __name__ == "__main__":
    unittest.main()
