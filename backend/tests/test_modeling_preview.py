"""Intermediate display updates must not accept or modify the editable world."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from review_reconstruction import Workbench
from core import APIError, SceneStore
from cycles_preview import published_build
from test_glb_validation import make_glb
from world_build import file_hash
from world_editor import editor_state, edit_transform
from world_model import WorldStore, scene_hash
from world_service import WorldService, displayed_preview

EXAMPLE = Path(__file__).resolve().parents[2] / 'examples/physical_world/scene.json'


class ModelingPreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.store = SceneStore(self.project / 'data')
        self.service = WorldService(self.project, self.store)
        self.original = json.loads(EXAMPLE.read_text())
        self.service.world.initialize(self.original)
        self.candidate = deepcopy(self.original)
        self.candidate['revision'] += 1
        self.candidate['objects'][0]['position'][0] += 1
        self.directory = self.project / 'world/builds/fixture'
        self.directory.mkdir(parents=True)
        nodes = [{'extras': {'stable_id': obj['id']}} for obj in self.candidate['objects']]
        (self.directory / 'scene.glb').write_bytes(make_glb({'asset': {'version': '2.0'}, 'scene': 0,
            'scenes': [{'nodes': list(range(len(nodes)))}], 'nodes': nodes}))
        (self.directory / 'scene.blend').write_bytes(b'fixture Blender source')
        (self.directory / 'job.json').write_text(json.dumps({'scene': self.candidate}))
        self.receipt = {'revision': self.candidate['revision'], 'scene_hash': scene_hash(self.candidate),
            'build_dir': str(self.directory), 'glb_path': str(self.directory / 'scene.glb'),
            'blend_path': str(self.directory / 'scene.blend'),
            'files': {name: file_hash(self.directory / name) for name in ('scene.glb', 'scene.blend')}}
        self.save_receipt()

    def save_receipt(self):
        (self.directory / 'receipt.json').write_text(json.dumps(self.receipt))

    def preview(self, revision=None):
        return self.service.call('workspace_preview_world', {'receipt_path': 'world/builds/fixture/receipt.json',
            'expected_scene_revision': self.store.scene()['revision'] if revision is None else revision,
            'summary': '床架与床头已完成，床品待建模'})

    def test_preview_is_selectable_and_cycles_bound_but_not_world_edit_or_acceptance(self):
        before = (self.project / 'world/scene.json').read_bytes()
        result = self.preview()
        self.assertFalse(result['preview']['accepted'])
        self.assertEqual(displayed_preview(self.store)['id'], result['preview']['id'])
        shown, build = published_build(self.project, self.store)
        self.assertEqual(build['draft_scene'], self.candidate)
        state = editor_state(self.project, self.store)
        self.assertTrue(state['matches_display'])
        self.assertFalse(state['editable'])
        self.assertEqual(state['objects'][0]['position'], self.candidate['objects'][0]['position'])
        obj = state['objects'][0]
        with self.assertRaises(APIError):
            edit_transform(self.project, self.store, {'expected_revision': state['revision'],
                'expected_scene_revision': shown['revision'], 'object_id': obj['id'],
                'position': obj['position'], 'rotation': obj['rotation']})
        self.assertEqual((self.project / 'world/scene.json').read_bytes(), before)
        self.assertFalse((self.project / 'reconstruction/status.json').exists())

    def test_duplicate_build_does_not_bump_display_revision(self):
        first = self.preview()
        second = self.preview()
        self.assertEqual(first, second)

    def test_stale_display_and_changed_build_leave_current_preview_untouched(self):
        first = self.preview()
        with self.assertRaises(APIError):
            self.preview(first['scene']['revision'] - 1)
        (self.directory / 'scene.blend').write_bytes(b'changed')
        with self.assertRaises(APIError):
            self.preview()
        self.assertEqual(self.store.scene(), first['scene'])

    def test_snapshot_mismatch_and_outside_project_are_rejected(self):
        (self.directory / 'job.json').write_text(json.dumps({'scene': self.original}))
        with self.assertRaises(APIError):
            self.preview()
        with self.assertRaises(APIError):
            self.service.call('workspace_preview_world', {'receipt_path': '/etc/passwd',
                'expected_scene_revision': 1, 'summary': 'invalid'})

    def test_normal_publication_clears_draft_label(self):
        result = self.preview()
        accepted = self.service._save_record('receipts', self.receipt)
        self.store.set_scene_preview(self.receipt['glb_path'], expected_revision=result['scene']['revision'])
        self.assertIsNone(displayed_preview(self.store))
        self.assertEqual(published_build(self.project, self.store)[1]['id'], accepted['id'])

    def test_controller_updates_each_completed_build_once_without_extra_agent_tasks(self):
        client = object.__new__(Workbench)
        updates = []
        statuses = iter(['running', 'completed'])
        def call(path, payload=None):
            if payload is None:
                return {'scene_revision': 1, 'queue': [{'feedback_id': 'builder', 'status': next(statuses)}]}
            self.assertEqual(path, '/api/workspace/reconstruction/preview')
            updates.append(payload)
            return {}
        client.call = call
        with patch('review_reconstruction.time.sleep'):
            client.wait_builder('builder', preview_project=self.project)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['receipt_path'], 'world/builds/fixture/receipt.json')

    def test_interrupted_builder_does_not_keep_waiting_or_submit_more_work(self):
        client = object.__new__(Workbench)
        client.call = lambda path: {'queue': [{'feedback_id': 'builder', 'status': 'interrupted'}]}
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            client.wait_builder('builder', preview_project=self.project)


if __name__ == '__main__':
    unittest.main()
