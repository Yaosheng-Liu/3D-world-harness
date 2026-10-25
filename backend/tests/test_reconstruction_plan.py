"""Inventory continuity, evidence isolation, edit hierarchy and workbench images."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image
from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reconstruction_plan import (candidate_coverage, file_hash, planning_image,
    unresolved_objects, validate_geometry, validate_plan, validate_segments)


class ReconstructionPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        frames = []
        for index in (1, 7):
            relative = f"observations/train/{index:06d}.png"
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (20, 20), (80, 100, 120)).save(path)
            frames.append({"index": index, "path": relative, "sha256": file_hash(path)})
        self.write("observations/manifest.json", {"train": frames, "heldout": []})
        self.plan = {"schema_version": 1,
            "source_manifest_sha256": file_hash(self.project / "observations/manifest.json"),
            "summary": "Separately editable cabinet and drawer", "unexplained_regions": [], "excluded_objects": [],
            "objects": [self.object("furniture", kind="group", strategy="group", observations=[]),
                        self.object("cabinet", parent="furniture"),
                        self.object("drawer", parent="cabinet", kind="part", strategy="model", observations=[1])]}
        self.plan_path = "object_pipeline/run/plan.json"
        self.write(self.plan_path, self.plan)
        mask = Image.new("L", (20, 20), 0)
        mask.paste(255, (2, 2, 10, 10))
        mask_path = self.project / "object_pipeline/run/mask.png"
        mask.save(mask_path)
        self.segments = {"schema_version": 1, "plan_sha256": file_hash(self.project / self.plan_path),
                         "observations_manifest_sha256": self.plan["source_manifest_sha256"], "objects": []}
        for obj in self.plan["objects"]:
            self.segments["objects"].append({"id": obj["id"],
                "status": "not_required" if obj["kind"] == "group" else "segmented", "reason": "test evidence",
                "observations": [{"frame_index": row["frame_index"], "mask_path": "object_pipeline/run/mask.png",
                    "mask_sha256": file_hash(mask_path), "score": .9, "prompt_method": "text"}
                    for row in obj["observations"]]})
        self.segments_path = "object_pipeline/run/segments.json"
        self.write(self.segments_path, self.segments)
        self.write("object_pipeline/status.json", {"stage": "ready_to_model", "plan_path": self.plan_path,
                                                  "segments_path": self.segments_path})

    def write(self, relative, value):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def object(self, ident, *, parent=None, kind="object", strategy="fuse", observations=None):
        indices = [1, 7] if observations is None else observations
        return {"id": ident, "name": ident, "category": ident, "parent_id": parent, "kind": kind,
                "priority": 1, "geometry_strategy": strategy, "segmentation_prompt": ident,
                "observations": [{"frame_index": index, "box": [.1, .1, .9, .9], "description": "visible source object"}
                                 for index in indices],
                "uncertainty": "Back surface is unobserved", "acceptance_criteria": ["moves independently"]}

    def test_previous_ids_require_explicit_evidenced_exclusion(self):
        previous = copy.deepcopy(self.plan)
        previous["objects"].append(self.object("extra_lamp", strategy="model", observations=[1]))
        with self.assertRaisesRegex(ValueError, "cannot disappear"):
            validate_plan(self.project, self.plan, previous)
        self.plan["excluded_objects"] = [{"id": "extra_lamp", "reason": "same object mistaken for a second instance",
                                          "frame_indices": [1, 7]}]
        validate_plan(self.project, self.plan, previous)
        self.plan["excluded_objects"][0]["frame_indices"] = []
        with self.assertRaises(ValidationError):
            validate_plan(self.project, self.plan, previous)
        self.plan["excluded_objects"][0]["frame_indices"] = [99]
        with self.assertRaisesRegex(ValueError, "original training evidence"):
            validate_plan(self.project, self.plan, previous)

    def test_excluded_ids_cannot_silently_disappear_on_next_revision(self):
        previous = copy.deepcopy(self.plan)
        previous["excluded_objects"] = [{"id": "duplicate_chair", "reason": "same chair in another view", "frame_indices": [1]}]
        with self.assertRaisesRegex(ValueError, "cannot disappear"):
            validate_plan(self.project, self.plan, previous)

    def test_invalid_hierarchy_and_group_strategy_are_rejected(self):
        for parent in ("unknown", "cabinet", "drawer"):
            plan = copy.deepcopy(self.plan)
            plan["objects"][1]["parent_id"] = parent
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, "cycle or missing parent"):
                validate_plan(self.project, plan)
        self.plan["objects"][1]["geometry_strategy"] = "group"
        with self.assertRaisesRegex(ValueError, "grouping nodes"):
            validate_plan(self.project, self.plan)

    def test_fusion_needs_two_distinct_training_views(self):
        self.plan["objects"][1]["observations"] = self.plan["objects"][1]["observations"][:1]
        with self.assertRaisesRegex(ValueError, "two distinct"):
            validate_plan(self.project, self.plan)
        self.plan["objects"][1]["observations"] *= 2
        with self.assertRaisesRegex(ValueError, "repeat an observation"):
            validate_plan(self.project, self.plan)

    def test_missing_objects_are_retained_and_reference_modeling_can_continue(self):
        for row in self.segments["objects"][1:]:
            row.update(status="missing", observations=[], reason="text and box did not yield a usable mask")
        validate_segments(self.project, self.plan_path, self.segments)
        pending = {row["object_id"]: row for row in unresolved_objects(self.plan, self.segments)}
        self.assertTrue(pending["cabinet"]["blocking"])
        self.assertFalse(pending["drawer"]["blocking"])
        self.segments["objects"].pop()
        with self.assertRaisesRegex(ValueError, "retain every planned object"):
            validate_segments(self.project, self.plan_path, self.segments)

    def test_masks_are_hash_bound_and_cannot_escape_the_project(self):
        mask = self.project / "object_pipeline/run/mask.png"
        original = mask.read_bytes()
        mask.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "mask bytes changed"):
            validate_segments(self.project, self.plan_path, self.segments)
        mask.write_bytes(original)
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "mask.png"
            outside.write_bytes(original)
            (self.project / "linked.png").symlink_to(outside)
            row = self.segments["objects"][1]["observations"][0]
            row["mask_path"] = "linked.png"
            with self.assertRaisesRegex(ValueError, "this project"):
                validate_segments(self.project, self.plan_path, self.segments)
            row["mask_path"] = str(outside)
            with self.assertRaisesRegex(ValueError, "project-relative"):
                validate_segments(self.project, self.plan_path, self.segments)

    def test_segment_manifest_must_still_match_current_observations(self):
        manifest = json.loads((self.project / "observations/manifest.json").read_text())
        manifest["source_changed"] = True
        self.write("observations/manifest.json", manifest)
        with self.assertRaises(ValueError):
            validate_segments(self.project, self.plan_path, self.segments)

    def test_masks_must_belong_to_the_objects_planned_views(self):
        self.segments["objects"][2]["observations"][0]["frame_index"] = 7
        with self.assertRaises(ValueError):
            validate_segments(self.project, self.plan_path, self.segments)

    def test_not_required_cannot_hide_a_real_object(self):
        self.segments["objects"][1].update(status="not_required", observations=[])
        with self.assertRaises(ValueError):
            validate_segments(self.project, self.plan_path, self.segments)

    def test_candidate_coverage_requires_objects_and_edit_hierarchy(self):
        scene = {"objects": [{"id": "cabinet", "parent_id": None}, {"id": "drawer", "parent_id": "cabinet"}]}
        self.assertTrue(candidate_coverage(self.plan, scene)["passed"])
        scene["objects"][1]["parent_id"] = None
        self.assertEqual(candidate_coverage(self.plan, scene)["hierarchy_mismatches"], ["drawer"])
        scene["objects"].pop()
        self.assertEqual(candidate_coverage(self.plan, scene)["missing_objects"], ["drawer"])

    def test_materialized_group_must_not_break_parent_chain(self):
        plan = copy.deepcopy(self.plan)
        plan["objects"].append(self.object("drawer_group", parent="cabinet", kind="group", strategy="group", observations=[]))
        plan["objects"][2]["parent_id"] = "drawer_group"
        scene = {"objects": [{"id": "cabinet", "parent_id": None}, {"id": "drawer_group", "parent_id": None},
                             {"id": "drawer", "parent_id": "drawer_group"}]}
        self.assertFalse(candidate_coverage(plan, scene)["passed"])
        scene["objects"][1]["parent_id"] = "cabinet"
        self.assertTrue(candidate_coverage(plan, scene)["passed"])

    def test_unexplained_regions_block_completion(self):
        self.plan["unexplained_regions"] = [{"frame_index": 1, "box": [.1, .1, .2, .2], "description": "unidentified object"}]
        self.assertTrue(unresolved_objects(self.plan)[0]["blocking"])
        scene = {"objects": [{"id": "cabinet", "parent_id": None}, {"id": "drawer", "parent_id": "cabinet"}]}
        self.assertFalse(candidate_coverage(self.plan, scene)["passed"])

    def test_planning_images_serve_original_or_verified_mask_overlay(self):
        source = planning_image(self.project, "cabinet", "1", "reference")
        self.assertEqual(source, self.project / "observations/train/000001.png")
        overlay = Image.open(io.BytesIO(planning_image(self.project, "cabinet", "1", "mask")))
        self.assertEqual(overlay.size, (20, 20))
        self.assertEqual(overlay.getpixel((15, 15)), (80, 100, 120))
        self.assertNotEqual(overlay.getpixel((5, 5)), (80, 100, 120))
        for ident, view, kind in (("../secret", "1", "reference"), ("cabinet", "99", "reference"),
                                 ("cabinet", "1", "../mask.png")):
            with self.subTest(ident=ident, view=view, kind=kind), self.assertRaises(ValueError):
                planning_image(self.project, ident, view, kind)

    def test_planning_images_reject_changed_masks_and_source_bytes(self):
        (self.project / "object_pipeline/run/mask.png").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "mask bytes changed"):
            planning_image(self.project, "cabinet", "1", "mask")
        (self.project / "observations/train/000001.png").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "reference image bytes changed"):
            planning_image(self.project, "cabinet", "1", "reference")

    def geometry(self):
        mesh = self.project / "object_pipeline/run/cabinet.obj"
        mesh.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        texture = self.project / "object_pipeline/run/mask.png"
        mesh_path, texture_path = str(mesh.relative_to(self.project)), str(texture.relative_to(self.project))
        files = {mesh_path: file_hash(mesh), texture_path: file_hash(texture)}
        return {"schema_version": 1, "plan_sha256": file_hash(self.project / self.plan_path),
                "segments_sha256": file_hash(self.project / self.segments_path), "input_files": {}, "files": files,
                "objects": [{"id": "furniture", "status": "not_required", "files": {}},
                            {"id": "cabinet", "status": "ready", "mesh_path": mesh_path,
                             "texture_path": texture_path, "files": dict(files)},
                            {"id": "drawer", "status": "not_required", "files": {}}]}

    def test_geometry_must_retain_exactly_one_record_per_object(self):
        report = self.geometry()
        validate_geometry(self.project, self.plan_path, self.segments_path, report)
        for rows in (report["objects"][:-1], report["objects"] + [report["objects"][1]]):
            malformed = {**report, "objects": rows}
            with self.subTest(count=len(rows)), self.assertRaisesRegex(ValueError, "retain every planned object"):
                validate_geometry(self.project, self.plan_path, self.segments_path, malformed)

    def test_ready_geometry_requires_existing_hash_bound_mesh_and_texture(self):
        for field in ("mesh_path", "texture_path"):
            report = self.geometry()
            del report["objects"][1][field]
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, "hash-bound independent"):
                validate_geometry(self.project, self.plan_path, self.segments_path, report)
        report = self.geometry()
        mesh = self.project / report["objects"][1]["mesh_path"]
        mesh.unlink()
        with self.assertRaises(FileNotFoundError):
            validate_geometry(self.project, self.plan_path, self.segments_path, report)
        report = self.geometry()
        mesh.write_text("replaced geometry")
        with self.assertRaisesRegex(ValueError, "geometry evidence bytes changed"):
            validate_geometry(self.project, self.plan_path, self.segments_path, report)

    def test_fusion_object_cannot_claim_geometry_is_not_required(self):
        report = self.geometry()
        report["objects"][1]["status"] = "not_required"
        with self.assertRaisesRegex(ValueError, "cannot silently skip geometry"):
            validate_geometry(self.project, self.plan_path, self.segments_path, report)


if __name__ == "__main__":
    unittest.main()
