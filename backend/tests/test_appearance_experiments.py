"""Appearance policies remain distinct while inputs and experiment counts agree."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "backend"), str(REPO / "scripts"), str(REPO / "experiments/reconstruction")]
from common import matrix
from reconstruction_appearance import validate_appearance
from review_reconstruction import review_prompt
from world_model import WorldError, WorldStore, validate_scene


class AppearanceExperimentsTests(unittest.TestCase):
    def setUp(self):
        self.scene = json.loads((REPO / "examples/physical_world/scene.json").read_text())
        self.scene["lighting"] = {"environment": {"color": [.5, .5, .5], "strength": .2}, "lights": [
            {"id": "lamp", "type": "spot", "position": [0, 0, 1], "rotation": [0, 0, 0],
             "color": [1, .9, .8], "energy": 50, "parent_id": "block", "spot_size": .8}]}

    def test_edit_and_reopen_preserve_explicit_lighting_and_optics(self):
        self.scene["objects"][0]["material"].update(transmission=.7, ior=1.45, emission_strength=.2)
        with tempfile.TemporaryDirectory() as directory:
            store = WorldStore(directory)
            store.initialize(self.scene)
            store.edit(1, [{"op": "update", "object_id": "block", "fields": {"position": [1, 0, 1]}}])
            reopened = WorldStore(directory).read()
            self.assertEqual(reopened["lighting"], self.scene["lighting"])
            self.assertEqual(reopened["objects"][0]["material"]["transmission"], .7)

    def test_light_parents_and_controls_are_validated(self):
        for field, value in (("parent_id", "missing"), ("energy", -1), ("spot_size", 4)):
            candidate = deepcopy(self.scene)
            candidate["lighting"]["lights"][0][field] = value
            with self.subTest(field=field), self.assertRaises(WorldError):
                validate_scene(candidate)

    def test_clean_regime_rejects_unlit_material_but_captured_accepts_it(self):
        self.scene["objects"][0]["material"]["appearance"] = "captured"
        validate_appearance(self.scene, "captured")
        with self.assertRaisesRegex(ValueError, "surface"):
            validate_appearance(self.scene, "intrinsic")
        self.scene["objects"][0]["material"]["appearance"] = "surface"
        validate_appearance(self.scene, "intrinsic")
        del self.scene["lighting"]
        with self.assertRaisesRegex(ValueError, "lighting"):
            validate_appearance(self.scene, "intrinsic")

    def test_reviewer_does_not_ban_captured_shadows_in_captured_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"candidate_id": "candidate", "plan_path": "plan.json", "segments_path": "segments.json",
                        "geometry_path": "geometry.json", "modeling_report_path": "modeling.json", "inventory_path": "inventory.json"}
            (root / "candidate.json").write_text(json.dumps(manifest))
            captured = review_prompt(root, root, None, appearance_regime="captured")
            intrinsic = review_prompt(root, root, None, appearance_regime="intrinsic")
            self.assertIn("do not require removing baked shadows", captured)
            self.assertNotIn("Do not prescribe baking shadows", captured)
            self.assertIn("Do not prescribe baking shadows", intrinsic)
            self.assertIn("LOCAL GEOMETRY RECONSTRUCTION", captured)

    def test_ultra_only_matrix_has_no_duplicate_high_trials(self):
        protocol = json.loads((REPO / "experiments/reconstruction/protocol.json").read_text())
        protocol["ablation_effort"] = None
        rows = matrix(protocol)
        self.assertEqual(len(rows), 6)
        self.assertEqual({row["effort"] for row in rows}, {"ultra"})
        self.assertEqual(len(protocol["scenes"]) * len(rows) * 2, 36)


if __name__ == "__main__":
    unittest.main()
