"""Full-frame image metrics, provenance binding, and explicit inspection writes."""

import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from world_evaluation import evaluate_views, write_view_inspection
from world_model import WorldError, scene_hash


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorldEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.output = self.project / "world" / "builds" / "frozen"
        self.output.mkdir(parents=True)
        self.reference = self.project / "reference.png"
        self.render = self.output / "heldout.png"
        self.scene = {"revision": 1, "views": [{"id": "heldout", "reference_image": "reference.png",
                                               "intrinsics": {"width": 2, "height": 1},
                                               "camera_to_world": [[1, 0, 0, 0], [0, 1, 0, 0],
                                                                   [0, 0, 1, 0], [0, 0, 0, 1]]}]}
        Image.new("RGB", (2, 1), "black").save(self.reference)
        Image.new("RGB", (2, 1), "black").save(self.render)
        self.bind()

    def tearDown(self):
        self.temporary.cleanup()

    def bind(self):
        self.build = {"scene_hash": scene_hash(self.scene), "build_dir": str(self.output),
                      "render_paths": [str(self.render)],
                      "asset_hashes": {"reference.png": digest(self.reference)},
                      "files": {"heldout.png": digest(self.render)}}

    def test_identical_images_are_json_safe_and_evaluation_is_read_only(self):
        before = {str(path.relative_to(self.project)): path.read_bytes()
                  for path in self.project.rglob("*") if path.is_file()}
        report = evaluate_views(self.project, self.scene, self.build)
        self.assertEqual(report["status"], "measured")
        self.assertEqual(report["aggregate"]["mae"], 0)
        self.assertEqual(report["aggregate"]["mse"], 0)
        self.assertIsNone(report["aggregate"]["psnr_db"])
        self.assertIsNone(report["views"][0]["alpha_coverage"])
        json.dumps(report, allow_nan=False)
        after = {str(path.relative_to(self.project)): path.read_bytes()
                 for path in self.project.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_known_pixel_error_counts_all_channels_and_pixels(self):
        image = Image.new("RGB", (2, 1), "black")
        image.putpixel((0, 0), (255, 0, 0))
        image.save(self.render)
        self.bind()
        metrics = evaluate_views(self.project, self.scene, self.build)["aggregate"]
        self.assertAlmostEqual(metrics["mae"], 1 / 6)
        self.assertAlmostEqual(metrics["mse"], 1 / 6)
        self.assertAlmostEqual(metrics["psnr_db"], 10 * math.log10(6))
        self.assertEqual(metrics["rgb_component_count"], 6)

    def test_transparent_holes_still_contribute_to_error_on_white_background(self):
        image = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
        image.putpixel((1, 0), (0, 0, 0, 0))
        image.save(self.render)
        self.bind()
        report = evaluate_views(self.project, self.scene, self.build)
        self.assertEqual(report["views"][0]["alpha_coverage"], 0.5)
        self.assertEqual(report["aggregate"]["mae"], 0.5)
        self.assertEqual(report["aggregate"]["mse"], 0.5)
        self.assertEqual(report["method"]["background_rgb"], [1, 1, 1])

    def test_reference_or_render_changes_are_rejected(self):
        for path in (self.reference, self.render):
            with self.subTest(path=path):
                self.bind()
                Image.new("RGB", (2, 1), "white").save(path)
                with self.assertRaisesRegex(WorldError, "changed"):
                    evaluate_views(self.project, self.scene, self.build)
                Image.new("RGB", (2, 1), "black").save(path)

    def test_changed_camera_and_nonfinite_scene_are_rejected(self):
        changed = copy.deepcopy(self.scene)
        changed["views"][0]["camera_to_world"][0][3] = 0.1
        with self.assertRaisesRegex(WorldError, "built revision"):
            evaluate_views(self.project, changed, self.build)
        changed["views"][0]["camera_to_world"][0][3] = float("nan")
        with self.assertRaisesRegex(WorldError, "finite JSON"):
            evaluate_views(self.project, changed, self.build)

    def test_wrong_size_and_non_rgb_image_are_rejected(self):
        Image.new("RGB", (1, 1), "black").save(self.render)
        self.bind()
        with self.assertRaisesRegex(WorldError, "sizes"):
            evaluate_views(self.project, self.scene, self.build)
        float_image = self.project / "reference.tiff"
        Image.new("F", (2, 1), float("nan")).save(float_image)
        self.scene["views"][0]["reference_image"] = "reference.tiff"
        self.bind()
        self.build["asset_hashes"]["reference.tiff"] = digest(float_image)
        with self.assertRaisesRegex(WorldError, "8-bit"):
            evaluate_views(self.project, self.scene, self.build)

    def test_missing_images_are_not_measured_but_partial_render_count_is_an_error(self):
        empty = dict(self.build, render_paths=[])
        self.assertEqual(evaluate_views(self.project, self.scene, empty)["status"], "not_measured")
        no_reference = {"views": [{"id": "overview"}]}
        self.assertEqual(evaluate_views(self.project, no_reference, self.build)["status"], "not_measured")
        changed = copy.deepcopy(self.scene)
        changed["views"].append({"id": "overview"})
        self.build["scene_hash"] = scene_hash(changed)
        with self.assertRaisesRegex(WorldError, "count"):
            evaluate_views(self.project, changed, self.build)

    def test_export_writes_four_labelled_panels_and_json_only_when_requested(self):
        report = write_view_inspection(self.project, self.scene, self.build)
        self.assertEqual(len(report["inspection_paths"]), 1)
        path = Path(report["inspection_paths"][0])
        with Image.open(path) as image:
            self.assertEqual(image.size, (8, 25))
        saved = json.loads((self.output / "evaluation" / "summary.json").read_text())
        self.assertEqual(saved, report)

    def test_export_rejects_preexisting_file_symlink_escape(self):
        output = self.output / "evaluation"
        output.mkdir()
        outside = self.project / "keep.txt"
        outside.write_text("preserve original bytes")
        for filename in ("heldout.png", "summary.json"):
            with self.subTest(filename=filename):
                path = output / filename
                path.symlink_to(outside)
                with self.assertRaisesRegex(WorldError, "escapes"):
                    write_view_inspection(self.project, self.scene, self.build)
                self.assertEqual(outside.read_text(), "preserve original bytes")
                path.unlink()

    def test_export_rejects_output_symlink_escape(self):
        outside = self.project / "unrelated"
        outside.mkdir()
        (self.output / "evaluation").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(WorldError, "escapes"):
            write_view_inspection(self.project, self.scene, self.build)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
