"""Depth diagnostics must preserve camera scale, source binding and missing data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reconstruction_review import verify_files, write_json
from world_build import file_hash
from world_depth import CONVENTION, depth_panels, write_depth_inspection
from world_model import scene_hash


def reference_fixture(project):
    """Small calibrated cache used by unit tests and the real Blender check."""
    (project / "dense").mkdir()
    (project / "observations/train").mkdir(parents=True)
    observations = []
    for i in (1, 7):
        from PIL import Image
        path = project / f"observations/train/{i:06d}.png"
        Image.new("RGB", (8, 6), "white").save(path)
        observations.append({"index": i, "path": str(path.relative_to(project)), "sha256": file_hash(path)})
    write_json(project / "observations/manifest.json", {"train": observations})
    write_json(project / "reconstruction_report.json", {"source_to_world": np.eye(4).tolist(), "units": "arbitrary"})
    k = np.repeat(np.array([[[5, 0, 3], [0, 6, 2], [0, 0, 1]]], dtype=float), 2, axis=0)
    e = np.repeat(np.eye(4)[None], 2, axis=0)
    e[:, 1, 1] = e[:, 2, 2] = -1
    e[:, 2, 3] = 3
    np.savez_compressed(project / "dense/depths.npz", depth=np.full((2, 6, 8), 3.), confidence=np.ones((2, 6, 8)),
                        intrinsics=k, extrinsics=e, frame_indices=[1, 7])
    write_json(project / "dense/inference.json", {
        "depth_convention": CONVENTION, "training_indices": [1, 7],
        "depths_sha256": file_hash(project / "dense/depths.npz"),
        "prepared_manifest_sha256": file_hash(project / "observations/manifest.json")})


class DepthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        reference_fixture(self.project)
        self.scene = {"units": "arbitrary", "revision": 2}
        directory = self.project / "world/builds/test"
        directory.mkdir(parents=True)
        (directory / "scene.blend").write_bytes(b"frozen blender")
        write_json(directory / "job.json", {"scene": self.scene})
        self.build = {"scene_hash": scene_hash(self.scene), "build_dir": str(directory),
                      "blend_path": str(directory / "scene.blend"), "files": {"scene.blend": file_hash(directory / "scene.blend")},
                      "asset_hashes": {name: file_hash(self.project / name) for name in
                                       ("dense/inference.json", "observations/manifest.json", "reconstruction_report.json")}}

    def run_inspection(self, output="check", **kwargs):
        def blender(command, **unused):
            self.assertEqual(unused["timeout"], kwargs.get("timeout", 1800))
            job = json.loads(Path(command[-1]).read_text())
            self.assertEqual(job["width"], 8)
            self.assertEqual(job["intrinsics"][0][0][2], 3)
            np.savez_compressed(Path(job["output_dir"]) / "model_depth.npz",
                                depth=np.full((len(job["frame_indices"]), 6, 8), 4.), frame_indices=job["frame_indices"])
        with patch("world_depth.subprocess.run", side_effect=blender):
            return write_depth_inspection(self.project, self.scene, self.build, self.project / output, **kwargs)

    def test_batch_depth_inspection_can_wait_without_a_render_deadline(self):
        self.assertEqual(self.run_inspection(timeout=None)["status"], "measured")

    def test_direction_unknown_and_missing_surface_colors(self):
        reference = np.array([[3., 3., 3., 3., 3., np.nan]])
        model = np.array([[2., 4., 3., 0., 4., 4.]])
        confidence = np.array([[10., 10., 10., 10., 0., 10.]])
        panels, counts = depth_panels(reference, model, confidence, depth_max=6, error_limit=1)
        np.testing.assert_array_equal(np.asarray(panels[2])[0], [(35, 95, 220), (220, 45, 35), (255, 255, 255),
                                                               (200, 35, 200), (90, 90, 90), (90, 90, 90)])
        self.assertEqual(counts["compared_pixels"], 3)
        self.assertEqual(counts["missing_model_pixels"], 1)

    def test_scale_is_fixed_and_artifacts_are_bound(self):
        original = {name: file_hash(self.project / name) for name in ("dense/depths.npz", "world/builds/test/scene.blend")}
        report = self.run_inspection()
        self.assertAlmostEqual(report["error_limit"], .3)
        self.assertEqual([row["view_id"] for row in report["views"]], ["source_1", "source_7"])
        self.assertEqual(report["views"][0]["compared_pixels"], 48)
        verify_files(self.project, report["input_files"])
        verify_files(self.project, report["files"])
        self.assertEqual(original, {name: file_hash(self.project / name) for name in original})
        with self.assertRaises(FileExistsError):
            self.run_inspection()
        (self.project / report["inspection_paths"][0]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "changed"):
            verify_files(self.project, report["files"])

    def test_selected_frames_do_not_include_heldout_or_duplicate_frames(self):
        report = self.run_inspection(frames=[7])
        self.assertEqual([row["frame_index"] for row in report["views"]], [7])
        for frames in ([0], [1, 1], []):
            with self.subTest(frames=frames), self.assertRaisesRegex(ValueError, "training frames"):
                self.run_inspection("invalid", frames=frames)

    def test_changed_depth_or_candidate_rejected_before_blender(self):
        self.build["scene_hash"] = "another revision"
        with self.assertRaisesRegex(ValueError, "candidate"):
            self.run_inspection()
        self.build["scene_hash"] = scene_hash(self.scene)
        (self.project / "dense/depths.npz").write_bytes(b"changed reference")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.run_inspection()
        self.assertFalse((self.project / "check").exists())

    def test_absent_depth_explicit_and_wrong_coordinates_not_fitted(self):
        coordinates = self.project / "reconstruction_report.json"
        data = json.loads(coordinates.read_text()); data["source_to_world"][0][3] = 1
        write_json(coordinates, data)
        self.build["asset_hashes"]["reconstruction_report.json"] = file_hash(coordinates)
        with self.assertRaisesRegex(ValueError, "original DA3 world"):
            self.run_inspection()
        (self.project / "dense/inference.json").unlink()
        self.assertEqual(self.run_inspection()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
