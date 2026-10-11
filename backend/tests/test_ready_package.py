"""Compatibility checks for existing video workbench-ready packages."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ready_import import discover_ready_instances


class ReadyPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.instance = self.root / 'A'
        self.instance.mkdir()
        for name in ('dynamic_scene.glb', 'dynamic_scene.blend', 'frame.jpg'):
            (self.instance / name).write_bytes(b'discovery validates paths; importer validates bytes')
        (self.instance / 'camera_manifest.json').write_text(json.dumps({'frames': []}))
        self.sequence = {'fps': 24, 'duration_sec': 5,
                         'camera_manifest_path': 'camera_manifest.json',
                         'frames': [{'path': 'frame.jpg', 'time_sec': 0}]}
        self.marker = {'status': 'ready', 'name': 'A · Escalator',
                       'scene_glb': 'dynamic_scene.glb', 'editable_blend': 'dynamic_scene.blend',
                       'reference_sequence': 'reference_clip.json',
                       'camera_manifest': 'camera_manifest.json', 'world_up': 'Z',
                       'start_command': 'never execute this source command'}

    def tearDown(self):
        self.temporary.cleanup()

    def discover(self):
        (self.instance / 'reference_clip.json').write_text(json.dumps(self.sequence))
        (self.instance / 'workbench_ready.json').write_text(json.dumps(self.marker))
        return discover_ready_instances(self.root)

    def test_existing_package_keeps_source_paths_and_axis(self):
        result = self.discover()
        self.assertFalse(result.errors)
        self.assertEqual(len(result.instances), 1)
        ready = result.instances[0]
        self.assertEqual(ready.format, 'ready_package')
        self.assertEqual(ready.glb, self.instance / 'dynamic_scene.glb')
        self.assertEqual(ready.manifest, self.instance / 'reference_clip.json')
        self.assertEqual(ready.blend, self.instance / 'dynamic_scene.blend')
        self.assertEqual(ready.camera_manifest, self.instance / 'camera_manifest.json')
        self.assertEqual(ready.world_up, 'z')
        self.assertNotIn('start_command', ready.provenance())

    def test_nonready_package_is_skipped_even_without_assets(self):
        self.marker = {'status': 'building', 'scene_glb': 'missing.glb'}
        result = self.discover()
        self.assertEqual(result.instances, [])
        self.assertFalse(result.errors)
        self.assertEqual(len(result.skipped), 1)

    def test_explicit_schema_is_validated_and_paths_stay_scoped(self):
        self.marker['schema_version'] = 2
        self.assertIn('schema_version', self.discover().errors[0]['error'])
        self.marker.pop('schema_version')
        outside = self.root / 'outside.glb'
        outside.write_bytes(b'outside')
        self.marker['scene_glb'] = '../outside.glb'
        self.assertIn('inside', self.discover().errors[0]['error'])

    def test_conflicting_cameras_and_invalid_axis_are_rejected(self):
        (self.instance / 'different_camera.json').write_text('{}')
        self.marker['camera_manifest'] = 'different_camera.json'
        self.assertIn('different camera', self.discover().errors[0]['error'])
        self.marker['camera_manifest'] = 'camera_manifest.json'
        self.marker['world_up'] = 'X'
        self.assertIn('world_up', self.discover().errors[0]['error'])


if __name__ == '__main__':
    unittest.main()
