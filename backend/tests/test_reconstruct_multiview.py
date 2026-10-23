"""Split isolation and actual camera conversion checks; no model weights needed."""
import copy
import importlib.util
import unittest
import tempfile
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/reconstruct_multiview.py'
SPEC = importlib.util.spec_from_file_location('reconstruct_multiview', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReconstructionInputTests(unittest.TestCase):
    def manifest(self):
        train, heldout = MODULE.split_frames(309)
        return {'frame_count': 309, 'train_indices': train, 'heldout_indices': heldout,
                'frames': [{'index': i, 'role': 'train' if i in train else 'heldout'}
                           for i in sorted(train + heldout)]}

    def test_frozen_uniform_split_has_no_reused_images(self):
        train, heldout = MODULE.split_frames(309)
        self.assertEqual(heldout, [0, 62, 123, 185, 246, 308])
        self.assertEqual(len(train), 48)
        self.assertEqual(len(set(train)), 48)
        self.assertFalse(set(train) & set(heldout))
        self.assertEqual((train[0], train[-1]), (1, 307))
        MODULE.validate_split(self.manifest())

    def test_split_rejects_training_leakage_and_mislabeled_rows(self):
        manifest = self.manifest()
        leaked = copy.deepcopy(manifest)
        leaked['train_indices'][0] = leaked['heldout_indices'][0]
        with self.assertRaises(ValueError):
            MODULE.validate_split(leaked)
        manifest['frames'][0]['role'] = 'train'
        with self.assertRaises(ValueError):
            MODULE.validate_split(manifest)

    def test_too_few_unique_images_fail_explicitly(self):
        with self.assertRaises(ValueError):
            MODULE.split_frames(53)

    def test_source_paths_cannot_escape_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                MODULE.source_file(root, '../outside.png')
            with self.assertRaises(ValueError):
                MODULE.source_file(root, '/outside.png')
            (root / 'linked.png').symlink_to('/tmp/outside.png')
            with self.assertRaises(ValueError):
                MODULE.source_file(root, 'linked.png')

    def test_prepared_training_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'observations/train/000001.png'
            image.parent.mkdir(parents=True)
            image.write_bytes(b'prepared RGB bytes')
            (root / 'split_manifest.json').write_text('{}')
            (root / 'calibration_audit.json').write_text('{}')
            prepared = {'calibration_audit_sha256': MODULE.digest(root / 'calibration_audit.json'),
                        'heldout': [], 'split_manifest_sha256': MODULE.digest(root / 'split_manifest.json'),
                        'train': [{'index': 1, 'path': 'observations/train/000001.png',
                                   'sha256': MODULE.digest(image)}]}
            (root / 'observations/manifest.json').write_text(json.dumps(prepared))
            MODULE.prepared_inputs(root, {'train_indices': [1], 'heldout_indices': []})
            image.write_bytes(b'different prepared RGB bytes')
            with self.assertRaises(ValueError):
                MODULE.prepared_inputs(root, {'train_indices': [1], 'heldout_indices': []})

    def test_duplicate_faces_include_reversed_and_cyclic_vertex_order(self):
        import numpy as np
        faces = np.array([[0, 1, 2], [2, 1, 0], [1, 2, 0],
                          [0, 1, 3], [1, 0, 3], [2, 3, 4]])
        keep = MODULE.unique_triangle_indices(faces)
        np.testing.assert_array_equal(keep, [0, 3, 5])
        np.testing.assert_array_equal(faces[keep], [[0, 1, 2], [0, 1, 3], [2, 3, 4]])

    def uv_module(self):
        path = SCRIPT.parent / 'multiview_uv.py'
        spec = importlib.util.spec_from_file_location('multiview_uv', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_uv_refinement_keeps_shared_edges_and_surface_area(self):
        import numpy as np
        uv = self.uv_module()
        vertices = np.array([[0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1]], dtype=float)
        faces = np.array([[0, 1, 2], [1, 3, 2]])
        vertices, faces, sources = uv.subdivide_marked_faces(
            vertices, faces, np.array([3, 7]), np.array([True, False]))
        self.assertEqual(len(vertices), 7)
        self.assertEqual(len(faces), 6)
        self.assertEqual(list(np.bincount(sources)[[3, 7]]), [4, 2])
        triangles = vertices[faces]
        area = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])[:, 2]/2
        self.assertTrue(np.all(area > 0))
        self.assertAlmostEqual(float(area.sum()), 1.)
        self.assertEqual(len(MODULE.unique_triangle_indices(faces)), 6)

    def test_shared_edge_encoding_handles_large_int32_mesh_indices(self):
        import numpy as np
        uv = self.uv_module()
        vertices = np.zeros((100000, 3))
        vertices[90000:90003] = [[1, 2, 3], [3, 2, 3], [1, 4, 3]]
        faces = np.array([[90000, 90001, 90002]], dtype=np.int32)
        result, children, sources = uv.subdivide_marked_faces(
            vertices, faces, np.array([7]), np.array([True]))
        expected_midpoints = np.array([[2, 2, 3], [1, 3, 3], [2, 3, 3]])
        np.testing.assert_allclose(result[-3:], expected_midpoints)
        self.assertEqual(len(children), 4)
        self.assertTrue(np.all(sources == 7))

    def test_simplification_gate_rejects_training_coverage_loss(self):
        import numpy as np
        before = np.ones((2, 100))
        after = before.copy()
        after[0, :2] = np.inf
        report = MODULE.simplification_audit(before, after, [7, 27])
        self.assertTrue(report['passed'])
        after[1, :20] = np.inf
        report = MODULE.simplification_audit(before, after, [7, 27])
        self.assertFalse(report['passed'])
        self.assertAlmostEqual(report['maximum_observed_coverage_drop'], .2)

    def test_projective_uv_error_is_bounded_without_moving_the_plane(self):
        import numpy as np
        uv = self.uv_module()
        vertices = np.array([[0, 0, 1], [1, 0, 3], [0, 1, 3]], dtype=float)
        faces, sources = np.array([[0, 1, 2]]), np.array([0])
        E, K = np.eye(4)[None], np.diag([100., 100., 1.])[None]
        vertices, faces, sources, report = uv.refine_projective_uv(vertices, faces, sources, E, K)
        self.assertGreater(report['max_error_px_before'], 1.)
        self.assertLessEqual(report['max_error_px_after'], .5)
        np.testing.assert_allclose(vertices[:, 2], 1 + 2*vertices[:, 0] + 2*vertices[:, 1])
        self.assertTrue(np.all(sources == 0))

    def camera_data(self, matrix):
        return {'fl_x': 400, 'fl_y': 500, 'cx': 300, 'cy': 200,
                'w': 960, 'h': 540, 'camera_model': 'OPENCV',
                'frames': [{'transform_matrix': matrix}]}

    def test_opengl_conversion_preserves_principal_point_and_vertical_sign(self):
        import numpy as np
        camera = [[1, 0, 0, 2], [0, 1, 0, 3], [0, 0, 1, 4], [0, 0, 0, 1]]
        _, extrinsics, intrinsics = MODULE.camera_arrays(self.camera_data(camera), [0])
        point = (extrinsics[0] @ np.array([3, 4, 2, 1]))[:3]
        pixel = intrinsics[0] @ point
        np.testing.assert_allclose(pixel[:2]/pixel[2], [500, -50])
        self.assertLess((extrinsics[0] @ np.array([2, 3, 5, 1]))[2], 0)

    def test_opengl_conversion_keeps_camera_roll(self):
        import numpy as np
        camera = [[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        _, extrinsics, intrinsics = MODULE.camera_arrays(self.camera_data(camera), [0])
        point = (extrinsics[0] @ np.array([-1, 2, -2, 1]))[:3]
        pixel = intrinsics[0] @ point
        np.testing.assert_allclose(pixel[:2]/pixel[2], [700, -50])

    def test_unsupported_camera_calibration_is_rejected(self):
        camera = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        for change in ({'camera_model': 'OPENCV_FISHEYE'}, {'k4': .01}):
            data = self.camera_data(camera)
            data.update(change)
            with self.assertRaises(ValueError):
                MODULE.camera_arrays(data, [0])
        for change in ({'fl_x': 500}, {'k1': .01}):
            data = self.camera_data(camera)
            # Unselected cameras must not silently use the wrong global K either.
            data['frames'].append({'transform_matrix': camera, **change})
            with self.assertRaises(ValueError):
                MODULE.camera_arrays(data, [0])


if __name__ == '__main__':
    unittest.main()
