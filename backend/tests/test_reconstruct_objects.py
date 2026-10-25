"""Instance identity, occlusion, texture ownership and actual TSDF integration."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/reconstruct_objects.py"
SPEC = importlib.util.spec_from_file_location("reconstruct_objects", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ObjectGeometryTests(unittest.TestCase):
    def scene(self):
        h, w = 30, 40
        depth = np.full((3, h, w), 2., dtype=np.float32)
        K = np.repeat(np.array([[40., 0., 20.], [0., 40., 15.], [0., 0., 1.]])[None], 3, axis=0)
        E = np.repeat(np.eye(4)[None], 3, axis=0)
        E[1, 0, 3], E[2, 0, 3] = -.1, .1
        masks = np.zeros_like(depth, dtype=bool)
        masks[0, 6:24, 12:28] = True
        masks[1, 6:24, 10:26] = True
        masks[2, 6:24, 14:30] = True
        return depth, masks, K, E

    def test_consistent_instance_uses_shared_cameras(self):
        depth, masks, K, E = self.scene()
        filtered, report = MODULE.consistent_object_depths(depth, masks, K, E, .005)
        np.testing.assert_array_equal(filtered > 0, masks)
        self.assertTrue(all(row["conflicting_pixels"] == 0 for row in report))

    def test_different_instance_in_other_view_is_rejected(self):
        depth, masks, K, E = self.scene()
        masks[1:] = False
        masks[1:, :, :6] = True
        filtered, report = MODULE.consistent_object_depths(depth[:2], masks[:2], K[:2], E[:2], .005)
        self.assertFalse(filtered.any())
        self.assertGreater(report[0]["conflicting_pixels"], 0)

    def test_occluded_view_is_not_a_visible_identity_conflict(self):
        depth, masks, K, E = self.scene()
        depth[2] = 1.
        masks[2] = False
        filtered, report = MODULE.consistent_object_depths(depth, masks, K, E, .005)
        np.testing.assert_array_equal(filtered[:2] > 0, masks[:2])
        self.assertEqual(report[0]["conflicting_pixels"], 0)
        self.assertGreater(report[0]["occluded_in_other_views_pixels"], 0)

    def test_single_view_is_retained_as_insufficient_not_invented(self):
        depth, masks, K, E = self.scene()
        filtered, report = MODULE.consistent_object_depths(depth[:1], masks[:1], K[:1], E[:1], .005)
        self.assertFalse(filtered.any())
        self.assertEqual(report[0]["unsupported_pixels"], int(masks[0].sum()))

    def test_part_takes_ancestor_pixels_and_peer_overlap_stays_unknown(self):
        parent = np.ones((5, 6), dtype=bool)
        part, peer = np.zeros_like(parent), np.zeros_like(parent)
        part[1:3, 1:3], peer[:, 5] = True, True
        result, report = MODULE.owned_masks(
            {"cabinet": {7: parent}, "drawer": {7: part}, "lamp": {7: peer}},
            {"cabinet": None, "drawer": "cabinet", "lamp": None})
        self.assertFalse((result["cabinet"][7] & part).any())
        self.assertFalse((result["cabinet"][7] & peer).any())
        self.assertFalse(result["lamp"][7].any())
        np.testing.assert_array_equal(result["drawer"][7], part)
        self.assertEqual(report["cabinet"]["part_pixels"], 4)
        self.assertEqual(report["lamp"]["conflicting_pixels"], 5)

    def test_parent_cycle_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            MODULE.owned_masks({"a": {}, "b": {}}, {"a": "b", "b": "a"})

    def test_mask_alignment_respects_crop_encoded_in_intrinsics(self):
        mask = np.zeros((12, 16), dtype=bool)
        mask[4:8, 6:10] = True
        full = np.array([[10., 0., 8.], [0., 10., 6.], [0., 0., 1.]])
        dense = np.array([[10., 0., 4.], [0., 10., 3.], [0., 0., 1.]])
        result = MODULE.align_mask(mask, full, dense, (8, 6))
        expected = np.zeros((6, 8), dtype=bool)
        expected[1:5, 2:6] = True
        np.testing.assert_array_equal(result, expected)

    def test_texture_contains_no_neighbor_color(self):
        images = np.zeros((1, 8, 10, 3), dtype=np.uint8)
        images[:] = [255, 0, 0]
        images[:, 2:6, 3:7] = [0, 0, 255]
        mask = np.zeros((1, 8, 10), dtype=bool)
        mask[:, 2:6, 3:7] = True
        atlas, _, _, _ = MODULE.masked_atlas(images, mask)
        colors = {tuple(color) for color in atlas.reshape(-1, 3)}
        self.assertEqual(colors, {(0, 0, 255), (110, 110, 110)})

    def test_explicit_uv_limits_have_a_specific_value_error_subclass(self):
        from multiview_uv import refine_projective_uv
        vertices = np.array([[0., 0., 1.], [1., 0., 3.], [0., 1., 3.]])
        faces, sources = np.array([[0, 1, 2]]), np.array([0])
        E, K = np.eye(4)[None], np.diag([100., 100., 1.])[None]
        self.assertTrue(issubclass(MODULE.UVRefinementLimit, ValueError))
        for limits in ({"max_faces": 0}, {"max_iterations": 0}, {"max_faces": 1}):
            with self.subTest(limits=limits), self.assertRaises(MODULE.UVRefinementLimit):
                refine_projective_uv(vertices, faces, sources, E, K, **limits)

    @unittest.skipUnless(importlib.util.find_spec("open3d"), "Open3D runtime required")
    def test_real_tsdf_creates_independent_plane_without_background(self):
        import cv2
        import open3d as o3d
        depth, masks, K, E = self.scene()
        images = np.zeros((*depth.shape, 3), dtype=np.uint8)
        images[:] = [0, 255, 0]
        images[masks] = [255, 0, 0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info, report = MODULE.fuse_object(
                {"depth": depth, "images": images, "intrinsics": K, "extrinsics": E}, masks, root, .025)
            self.assertGreater(info["triangles"], 10)
            mesh = o3d.io.read_triangle_mesh(str(root / "observed.obj"))
            vertices = np.asarray(mesh.vertices)
            self.assertLess(float(abs(vertices[:, 2] - 2).max()), .03)
            self.assertLess(float(abs(vertices[:, 0]).max()), .45)
            atlas = cv2.imread(str(root / "captured.png"))
            self.assertFalse(np.any(np.all(atlas == [0, 255, 0], axis=2)))
            self.assertTrue(all(row["retained_pixels"] > 0 for row in report))

    def fixture(self, root):
        import cv2
        source, project = root / "source", root / "project"
        source.mkdir()
        (project / "observations/train").mkdir(parents=True)
        (project / "observations/heldout").mkdir()
        (project / "dense").mkdir()
        depth, masks, K, E = self.scene()
        images = np.full((*depth.shape, 3), 170, dtype=np.uint8)
        cameras = np.linalg.inv(E) @ np.diag([1., -1., -1., 1.])
        data = {"fl_x": 40., "fl_y": 40., "cx": 20., "cy": 15., "w": 40, "h": 30,
                "frames": [{"file_path": f"{i}.png", "transform_matrix": camera.tolist()}
                           for i, camera in enumerate(cameras)]}
        MODULE.write_json(source / "transforms.json", data)
        entries, prepared = [], {"train": [], "heldout": []}
        for i in range(3):
            cv2.imwrite(str(source / f"{i}.png"), images[i])
            role = "train" if i < 2 else "heldout"
            relative = f"observations/{role}/{i:06d}.png"
            cv2.imwrite(str(project / relative), images[i])
            entries.append({"index": i, "role": role, "path": f"{i}.png", "sha256": MODULE.digest(source / f"{i}.png")})
            prepared[role].append({"index": i, "path": relative, "sha256": MODULE.digest(project / relative)})
        split = {"source_directory": str(source), "source_transforms_sha256": MODULE.digest(source / "transforms.json"),
                 "frame_count": 3, "train_indices": [0, 1], "heldout_indices": [2], "frames": entries}
        MODULE.write_json(project / "split_manifest.json", split)
        MODULE.write_json(project / "calibration_audit.json", {})
        prepared.update(split_manifest_sha256=MODULE.digest(project / "split_manifest.json"),
                        calibration_audit_sha256=MODULE.digest(project / "calibration_audit.json"))
        MODULE.write_json(project / "observations/manifest.json", prepared)
        prepared_hash = MODULE.digest(project / "observations/manifest.json")
        np.savez_compressed(project / "dense/depths.npz", depth=depth[:2], confidence=np.full_like(depth[:2], 3),
                            images=images[:2], intrinsics=K[:2], extrinsics=E[:2], frame_indices=[0, 1])
        MODULE.write_json(project / "dense/inference.json", {"prepared_manifest_sha256": prepared_hash,
                         "depths_sha256": MODULE.digest(project / "dense/depths.npz")})
        plan = {"source_manifest_sha256": prepared_hash,
                "objects": [{"id": "plane", "kind": "object", "parent_id": None, "geometry_strategy": "fuse"},
                            {"id": "missing", "kind": "part", "parent_id": "plane", "geometry_strategy": "fuse"}]}
        MODULE.write_json(project / "plan.json", plan)
        observations = []
        for i in range(2):
            path = project / f"mask_{i}.png"
            cv2.imwrite(str(path), masks[i].astype(np.uint8) * 255)
            observations.append({"frame_index": i, "mask_path": path.name, "mask_sha256": MODULE.digest(path)})
        segments = {"plan_sha256": MODULE.digest(project / "plan.json"), "observations_manifest_sha256": prepared_hash,
                    "objects": [{"id": "plane", "status": "segmented", "observations": observations},
                                {"id": "missing", "status": "missing", "observations": []}]}
        MODULE.write_json(project / "segments.json", segments)
        return project, segments

    def test_hash_mismatch_and_heldout_masks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project, segments = self.fixture(Path(directory))
            segments["objects"][0]["observations"][0]["mask_sha256"] = "wrong"
            MODULE.write_json(project / "segments.json", segments)
            with self.assertRaisesRegex(ValueError, "Mask bytes"):
                MODULE.reconstruct(project, "plan.json", "segments.json", "result")
            segments["objects"][0]["observations"][0]["frame_index"] = 2
            MODULE.write_json(project / "segments.json", segments)
            with self.assertRaisesRegex(ValueError, "training images"):
                MODULE.reconstruct(project, "plan.json", "segments.json", "result")

    def test_unexpected_geometry_errors_still_fail_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _ = self.fixture(Path(directory))
            with patch.object(MODULE, "fuse_object", side_effect=ValueError("broken geometry data")):
                with self.assertRaisesRegex(ValueError, "broken geometry data"):
                    MODULE.reconstruct(project, "plan.json", "segments.json", "result")

    @unittest.skipUnless(importlib.util.find_spec("open3d"), "Open3D runtime required")
    def test_one_uv_budget_failure_preserves_other_actual_fused_objects(self):
        import cv2
        from multiview_uv import refine_projective_uv
        with tempfile.TemporaryDirectory() as directory:
            project, segments = self.fixture(Path(directory))
            plan = json.loads((project / "plan.json").read_text())
            plan["objects"].append({"id": "other", "kind": "object", "parent_id": None, "geometry_strategy": "fuse"})
            MODULE.write_json(project / "plan.json", plan)
            extra = {"id": "other", "status": "segmented", "observations": []}
            for order, row in enumerate(segments["objects"][0]["observations"]):
                path = project / row["mask_path"]
                mask = cv2.imread(str(path), 0)
                split = 20 - order * 2
                other = mask.copy(); other[:, :split] = 0
                mask[:, split:] = 0
                cv2.imwrite(str(path), mask)
                row["mask_sha256"] = MODULE.digest(path)
                other_path = project / f"other_{order}.png"
                cv2.imwrite(str(other_path), other)
                extra["observations"].append({"frame_index": order, "mask_path": other_path.name,
                                              "mask_sha256": MODULE.digest(other_path)})
            segments["objects"].append(extra)
            segments["plan_sha256"] = MODULE.digest(project / "plan.json")
            MODULE.write_json(project / "segments.json", segments)
            calls = 0

            def first_object_over_budget(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    kwargs["max_faces"] = 0
                return refine_projective_uv(*args, **kwargs)

            with patch("multiview_uv.refine_projective_uv", side_effect=first_object_over_budget):
                report = MODULE.reconstruct(project, "plan.json", "segments.json", "result")
            self.assertEqual([row["status"] for row in report["objects"]],
                             ["needs_revision", "insufficient_evidence", "ready"])
            self.assertIn("face limit is 0", report["objects"][0]["reason"])
            self.assertEqual(report["objects"][0]["files"], {})
            self.assertNotIn("mesh_path", report["objects"][0])
            self.assertGreater(report["objects"][2]["triangles"], 0)
            self.assertTrue((project / "result/geometry.json").is_file())

    @unittest.skipUnless(importlib.util.find_spec("open3d"), "Open3D runtime required")
    def test_run_retains_missing_objects_and_binds_independent_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            project, _ = self.fixture(Path(directory))
            report = MODULE.reconstruct(project, "plan.json", "segments.json", "result")
            self.assertEqual([row["status"] for row in report["objects"]], ["ready", "insufficient_evidence"])
            self.assertEqual(report["plan_sha256"], MODULE.digest(project / "plan.json"))
            for name, expected in report["files"].items():
                self.assertEqual(MODULE.digest(project / name), expected)
            self.assertTrue((project / "result/geometry.json").is_file())
            with self.assertRaisesRegex(ValueError, "new or empty"):
                MODULE.reconstruct(project, "plan.json", "segments.json", "result")


if __name__ == "__main__":
    unittest.main()
