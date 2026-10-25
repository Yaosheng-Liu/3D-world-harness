"""Fixed-camera protocol and per-object semantic-edit evidence binding."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from reconstruction_review import make_object_inspection_sheets, validate_fixed_views
from world_model import WorldStore


class FixedViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        Image.new("RGB", (64, 48), "gray").save(self.project / "reference.png")
        scene = json.loads((ROOT / "examples/physical_world/scene.json").read_text())
        scene["views"] = [{"id": "fixed", "reference_image": "reference.png",
                           "camera_to_world": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 4], [0, 0, 0, 1]],
                           "intrinsics": {"fx": 50, "fy": 50, "cx": 32, "cy": 24, "width": 64, "height": 48}}]
        self.base = WorldStore(self.project).initialize(scene)

    def test_existing_protocol_is_preserved_and_extra_views_are_allowed(self):
        candidate = deepcopy(self.base)
        candidate["views"].append({**deepcopy(candidate["views"][0]), "id": "additional"})
        validate_fixed_views(self.project, candidate)

    def test_removing_or_rebinding_a_fixed_view_fails(self):
        variants = []
        candidate = deepcopy(self.base); candidate["views"] = []; variants.append(candidate)
        candidate = deepcopy(self.base); candidate["views"][0]["camera_to_world"][0][3] += 1; variants.append(candidate)
        candidate = deepcopy(self.base); candidate["views"][0]["intrinsics"]["fx"] += 1; variants.append(candidate)
        candidate = deepcopy(self.base); candidate["views"][0]["reference_image"] = "generated.png"; variants.append(candidate)
        for candidate in variants:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, "fixed reference view"):
                validate_fixed_views(self.project, candidate)


class ObjectInspectionSheetTests(unittest.TestCase):
    def test_all_pairs_remain_accessible_and_cropped_sheets_include_both_states(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            output = project / "frozen"
            (output / "object_edits").mkdir(parents=True)
            rows = []
            for key in ("left", "right"):
                before = output / f"object_edits/{key}-before.png"
                hidden = output / f"object_edits/{key}-hidden.png"
                Image.new("RGBA", (80, 60), (255, 0, 0, 255)).save(before)
                Image.new("RGBA", (80, 60), (0, 0, 255, 255)).save(hidden)
                rows.append({"object_id": key, "status": "rendered", "view_id": "front",
                             "region": [.1, .1, .9, .9], "before_path": str(before), "hidden_path": str(hidden)})
            report = {"object_inspections": rows}
            sheets = make_object_inspection_sheets(project, output, report)
            self.assertEqual(report["inspection_paths"], ["frozen/edit_objects_01.png"])
            self.assertEqual(sheets, report["inspection_paths"])
            for row in rows:
                self.assertTrue(row["before_path"].startswith("frozen/object_edits/"))
                self.assertTrue((project / row["hidden_path"]).is_file())
            with Image.open(project / sheets[0]) as sheet:
                colors = {color for _, color in sheet.getcolors(sheet.width * sheet.height)}
                self.assertIn((255, 0, 0), colors)
                self.assertIn((0, 0, 255), colors)

    def test_objects_without_a_visible_view_remain_explicit_in_the_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            report = {"object_inspections": [{"object_id": "occluded", "status": "not_visible"}]}
            sheets = make_object_inspection_sheets(project, project, report)
            self.assertTrue((project / sheets[0]).is_file())
            self.assertEqual(report["object_inspections"][0]["status"], "not_visible")


if __name__ == "__main__":
    unittest.main()
