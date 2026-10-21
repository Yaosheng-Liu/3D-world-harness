"""CLI projects over HTTP and a real stdio transport; no account/model calls."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from appserver_adapter import AppServerError, CodexAppServerAdapter
import server as server_module
from test_appserver_adapter import FAKE_SERVER, until
import test_projects

CATALOG = [{"model": "fixture-vision", "displayName": "CLI 测试模型", "inputModalities": ["text", "image"],
            "supportedReasoningEfforts": [{"reasoningEffort": "high"}], "defaultReasoningEffort": "high", "isDefault": True},
           {"model": "alternate-vision", "displayName": "另一视觉模型", "inputModalities": ["text", "image"],
            "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}], "defaultReasoningEffort": "low"},
           {"model": "text-only", "inputModalities": ["text"]}]


class CLIProjectTests(unittest.TestCase):
    request = test_projects.ProjectTests.request
    stop_server = test_projects.ProjectTests.stop_server

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / 'workspace'
        self.project.mkdir()
        self.data = self.root / 'data'
        self.log = self.root / 'wire.jsonl'
        self.script = self.root / 'fake.py'
        source = FAKE_SERVER.replace('import json, sys', 'import json, sys, uuid, os\nthread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, os.getcwd()))')
        source = source.replace('"thread-test"', 'thread_id')
        source = source.replace('    elif method == "thread/start":', '    elif method == "model/list":\n        send({"id": request_id, "result": {"data": ' + repr(CATALOG) + ', "nextCursor": None}})\n    elif method == "thread/start":')
        self.script.write_text(source)
        def factory(*args, **kwargs):
            return CodexAppServerAdapter(*args, **kwargs, command=[sys.executable, '-u', str(self.script), str(self.log)], verify_version=False, request_timeout=2)
        self.adapter_patch = patch('appserver_adapter.CodexAppServerAdapter', side_effect=factory)
        self.adapter_patch.start()
        self.start_server()

    def start_server(self, *, enable_codex=True):
        self.server = server_module.make_server(port=0, project_dir=self.project, data_dir=self.data,
            web_dir=Path(__file__).resolve().parents[2] / 'web', enable_codex=enable_codex)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root_context = self.server.project_registry.root

    def tearDown(self):
        self.stop_server()
        self.adapter_patch.stop()
        self.temporary.cleanup()

    def payload(self, **changes):
        return dict(name='CLI 新场景', model='fixture-vision', reasoning_effort='high',
                    permission_mode='workspace_write', request_id=uuid.uuid4().hex, **changes)

    def create(self, payload):
        return self.request('POST', '/api/projects', payload, capability=self.root_context.store.browser_token)

    def wire(self, method):
        return [entry['params'] for entry in map(json.loads, self.log.read_text().splitlines()) if entry.get('method') == method]

    def ready_fixture(self):
        from PIL import Image
        from test_folder_import import glb_bytes
        source = self.root / 'import-source'
        source.mkdir()
        (source / 'scene.glb').write_bytes(glb_bytes())
        Image.new('RGB', (24, 24), 'blue').save(source / 'frame.png')
        (source / 'workbench-ready.json').write_text(json.dumps({
            'schema_version': 1, 'name': 'Imported room', 'glb': 'scene.glb',
            'reference_images': ['frame.png'],
        }))
        return source

    def import_source(self, source):
        status, result, _ = self.request('POST', '/api/projects/import-folder',
            {'path': str(source), 'request_id': uuid.uuid4().hex},
            capability=self.root_context.store.browser_token)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['errors'], [], result)
        return self.server.project_registry.get(result['projects'][0]['project_id'])

    def test_cli_import_connects_once_and_reuses_thread_after_restart_and_reload(self):
        source = self.ready_fixture()
        before = {p.name: p.read_bytes() for p in source.iterdir()}
        child = self.import_source(source)
        thread_id = child.store.workspace()['thread_id']
        self.assertTrue(thread_id)
        self.assertNotEqual(thread_id, self.root_context.store.workspace()['thread_id'])
        self.assertEqual(child.store.workspace()['agent']['status'], 'idle')
        self.assertTrue(child.gateway.state()['model_settings_supported'])
        self.assertEqual(len(self.wire('thread/start')), 2)
        self.assertEqual(self.wire('turn/start'), [])
        params = self.wire('thread/start')[-1]
        self.assertEqual(params['cwd'], str(source))
        self.assertEqual((params['sandbox'], params['approvalPolicy']), ('workspace-write', 'on-request'))
        env = params['config']['mcp_servers']['scene_feedback']['env']
        self.assertEqual(env['SCENE_FEEDBACK_DATA_DIR'], str(child.store.data_dir))
        self.assertEqual(self.import_source(source).store.workspace()['thread_id'], thread_id)
        project_id = child.project_id
        self.stop_server(); self.start_server()
        child = self.server.project_registry.get(project_id)
        self.assertEqual(child.store.workspace()['thread_id'], thread_id)
        self.assertEqual(len(self.wire('thread/start')), 2)
        self.assertEqual(self.request('DELETE', '/api/projects/' + project_id,
            capability=self.root_context.store.browser_token)[0], 200)
        self.assertEqual(self.import_source(source).store.workspace()['thread_id'], thread_id)
        self.assertEqual(len(self.wire('thread/start')), 2)
        self.assertEqual({p.name: p.read_bytes() for p in source.iterdir()}, before)

    def test_legacy_cli_import_backfills_connection_and_delivers_saved_feedback_once(self):
        self.stop_server(); self.start_server(enable_codex=False)
        child = self.import_source(self.ready_fixture())
        self.assertIsNone(child.gateway.adapter)
        session = child.store.workspace()['session_id']
        packet = child.gateway.submit(session, {'idempotency_key': 'legacy-import-feedback-0001',
            'scene_revision': child.store.scene()['revision'], 'note': 'Continue the room.'})
        project_id = child.project_id
        self.assertEqual(self.wire('turn/start'), [])
        self.stop_server(); self.start_server()
        child = self.server.project_registry.get(project_id)
        until(lambda: child.store.workspace()['queue'][-1]['status'] == 'completed')
        thread_id = child.store.workspace()['thread_id']
        self.assertEqual(child.store.workspace()['session_id'], session)
        self.assertEqual(child.store.workspace()['queue'][-1]['feedback_id'], packet['feedback_id'])
        self.assertEqual(len(self.wire('turn/start')), 1)
        self.assertEqual(self.wire('turn/start')[0]['threadId'], thread_id)
        self.stop_server(); self.start_server()
        self.assertEqual(len(self.wire('turn/start')), 1)
        self.assertEqual(self.server.project_registry.get(project_id).store.workspace()['thread_id'], thread_id)

    def test_import_connection_failure_keeps_scene_and_does_not_recreate_unconfirmed_thread(self):
        source = self.ready_fixture()
        with patch.object(CodexAppServerAdapter, 'start', side_effect=RuntimeError('thread/start outcome unknown')):
            child = self.import_source(source)
        self.assertEqual(child.store.workspace()['agent']['status'], 'disconnected')
        self.assertTrue(child.store.scene()['objects'])
        self.assertTrue((child.store.data_dir / 'cli_import_connection.json').is_file())
        project_id = child.project_id
        self.stop_server(); self.start_server()
        child = self.server.project_registry.get(project_id)
        self.assertIsNone(child.gateway.adapter)
        self.assertIsNone(child.store.workspace()['thread_id'])
        self.assertEqual(child.store.workspace()['agent']['status'], 'disconnected')
        self.assertIn('unconfirmed', child.store.workspace()['agent']['error'])
        self.assertEqual(len(self.wire('thread/start')), 1)
        self.assertTrue(child.store.scene()['objects'])

    def test_missing_import_thread_file_preserves_saved_conversation_identity(self):
        child = self.import_source(self.ready_fixture())
        project_id = child.project_id
        thread_id = child.store.workspace()['thread_id']
        thread_path = child.store.data_dir / 'codex_app_server_thread.json'
        self.stop_server()
        thread_path.unlink()
        self.start_server()
        child = self.server.project_registry.get(project_id)
        self.assertEqual(child.store.workspace()['thread_id'], thread_id)
        self.assertEqual(child.store.workspace()['agent']['status'], 'disconnected')
        self.assertIsNone(child.gateway.adapter)
        self.assertEqual(len(self.wire('thread/start')), 2)

    def test_create_switch_restart_and_deliver_with_selected_settings(self):
        root = self.root_context
        self.assertTrue(root.gateway.state()['project_creation_supported'])
        self.assertFalse(root.gateway.state()['desktop_available'])
        status, models, _ = self.request('GET', '/api/workspace/models')
        self.assertEqual(status, 200)
        self.assertEqual([m['model'] for m in models['models']], ['fixture-vision', 'alternate-vision'])
        before = root.store.state_path.read_bytes()
        payload = self.payload()
        status, result, _ = self.create(payload)
        self.assertEqual(status, 201, result)
        child = self.server.project_registry.get(result['project']['project_id'])
        self.assertNotEqual(root.store.workspace()['thread_id'], child.store.workspace()['thread_id'])
        self.assertNotEqual(root.store.browser_token, child.store.browser_token)
        self.assertEqual(root.store.state_path.read_bytes(), before)
        self.assertEqual(child.store.scene()['objects'], [])
        self.assertEqual(child.project_dir, self.data / 'projects' / child.project_id / 'workspace')
        prefix = '/p/' + child.project_id
        self.assertEqual(self.request('GET', prefix + '/api/workspace/state')[1]['session_id'], child.store.workspace()['session_id'])
        self.assertEqual(self.request('GET', '/api/workspace/state')[1]['session_id'], root.store.workspace()['session_id'])
        self.assertEqual(self.request('POST', prefix + '/api/projects', self.payload(), capability=root.store.browser_token)[0], 403)
        self.assertEqual(self.create(payload)[1]['project']['project_id'], child.project_id)
        self.assertEqual(len(self.wire('thread/start')), 2)
        child_id, thread_id = child.project_id, child.store.workspace()['thread_id']
        self.stop_server()
        self.start_server()
        child = self.server.project_registry.get(child_id)
        self.assertEqual(child.store.workspace()['thread_id'], thread_id)
        self.assertEqual(len(self.wire('thread/start')), 2)
        self.assertEqual(self.create(payload)[0], 201)
        adapter = child.gateway.adapter
        adapter.start_turn('fixture feedback')
        until(lambda: adapter.status()['turn_state'] == 'idle')
        turn = self.wire('turn/start')[-1]
        self.assertEqual((turn['threadId'], turn['model'], turn['effort']), (thread_id, 'fixture-vision', 'high'))
        self.assertEqual(turn['sandboxPolicy']['type'], 'workspaceWrite')
        resume = next(item for item in self.wire('thread/resume') if item['threadId'] == thread_id)
        self.assertEqual(resume['config']['model_reasoning_effort'], 'high')
        env = resume['config']['mcp_servers']['scene_feedback']['env']
        self.assertEqual(env['SCENE_FEEDBACK_PORT'], str(self.server.server_port))
        self.assertEqual(env['SCENE_FEEDBACK_PROJECT_DIR'], str(child.project_dir))
        self.assertEqual(env['SCENE_FEEDBACK_DATA_DIR'], str(child.store.data_dir))

    def test_change_model_preserves_thread_and_survives_restart(self):
        _, result, _ = self.create(self.payload())
        project_id = result['project']['project_id']
        child = self.server.project_registry.get(project_id)
        route = '/p/' + project_id + '/api/workspace/model-settings'
        thread_id = child.store.workspace()['thread_id']
        old_settings = child.gateway.state()['model_settings']
        self.assertTrue(child.gateway.state()['model_settings_supported'])
        # Persist a real turn first so this is an existing conversation.
        child.gateway.adapter.start_turn('first feedback')
        until(lambda: child.gateway.adapter.status()['turn_state'] == 'idle')
        payload = dict(model='alternate-vision', reasoning_effort=None, expected_settings=old_settings)
        self.assertEqual(self.request('POST', route, payload, capability=self.root_context.store.browser_token)[0], 403)
        # Changing only settings cannot alter scene, evidence, or session identity.
        scene_before = child.store.scene()
        session_id = child.store.workspace()['session_id']
        status, reply, _ = self.request('POST', route, payload, capability=child.store.browser_token)
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply['workspace']['model_settings'], dict(model='alternate-vision', reasoning_effort='low'))
        self.assertEqual(child.store.workspace()['thread_id'], thread_id)
        self.assertEqual(child.store.workspace()['session_id'], session_id)
        self.assertEqual(child.store.scene(), scene_before)
        self.assertEqual(self.request('POST', route, payload, capability=child.store.browser_token)[0], 409)
        child.gateway.adapter.start_turn('second feedback')
        until(lambda: child.gateway.adapter.status()['turn_state'] == 'idle')
        turn = self.wire('turn/start')[-1]
        self.assertEqual((turn['threadId'], turn['model'], turn['effort']), (thread_id, 'alternate-vision', 'low'))
        self.assertEqual(turn['sandboxPolicy']['type'], 'workspaceWrite')
        self.stop_server()
        self.start_server()
        child = self.server.project_registry.get(project_id)
        self.assertEqual(child.gateway.state()['model_settings'], dict(model='alternate-vision', reasoning_effort='low'))
        self.assertEqual(child.store.workspace()['thread_id'], thread_id)
        self.assertEqual(len(self.wire('thread/start')), 2)
        resume = [r for r in self.wire('thread/resume') if r['threadId'] == thread_id][-1]
        self.assertEqual(resume['model'], 'alternate-vision')
        self.assertEqual(resume['config']['model_reasoning_effort'], 'low')
        child.gateway.adapter.start_turn('after restart')
        until(lambda: child.gateway.adapter.status()['turn_state'] == 'idle')
        self.assertEqual(self.wire('turn/start')[-1]['model'], 'alternate-vision')
        # Project creation retries still use the original immutable creation spec.
        self.assertEqual(child.gateway.adapter.sandbox, 'workspace-write')

    def test_model_settings_validation_busy_and_failed_save(self):
        gateway = self.root_context.gateway
        adapter = gateway.adapter
        settings = gateway.state()['model_settings']
        payload = dict(model='alternate-vision', reasoning_effort='high', expected_settings=settings)
        route = '/api/workspace/model-settings'
        capability = self.root_context.store.browser_token
        def post(value):
            return self.request('POST', route, value, capability=capability)
        for change in [dict(model='text-only'), dict(model='missing'), dict(reasoning_effort='bad'), dict(reasoning_effort=[])]:
            self.assertEqual(post(dict(payload, **change))[0], 400)
        self.assertEqual(post(dict(payload, permission_mode='full_access'))[0], 400)
        for queue_status in ['queued', 'running', 'dispatching', 'delivery_uncertain']:
            with gateway.store.lock:
                gateway.store.state['workspace']['queue'] = [dict(feedback_id='pending', status=queue_status)]
            self.assertEqual(post(payload)[0], 409)
        with gateway.store.lock:
            gateway.store.state['workspace']['queue'] = []
        with patch.object(adapter, '_turn_state', 'running'):
            self.assertEqual(post(payload)[0], 409)
        with patch.object(gateway.store, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                gateway.update_model_settings(payload)
        self.assertEqual(gateway.state()['model_settings'], settings)
        self.assertEqual((adapter.model, adapter.reasoning_effort), (settings['model'], settings['reasoning_effort']))
        with patch.object(gateway, 'external_review', True):
            self.assertEqual(post(payload)[0], 409)
        with patch.object(gateway, 'blank_workbench', True, create=True):
            self.assertEqual(post(payload)[0], 409)

    def test_model_catalog_pagination_failure_and_disabled_cli(self):
        adapter = self.root_context.gateway.adapter
        pages = [{'data': [CATALOG[0]], 'nextCursor': 'page2'}, {'data': CATALOG[1:], 'nextCursor': None}]
        with patch.object(adapter, '_rpc', side_effect=pages) as rpc:
            self.assertEqual(adapter.list_models(), CATALOG)
            self.assertEqual(rpc.call_args_list[1].args[1]['cursor'], 'page2')
        with patch.object(adapter, '_rpc', return_value={'data': [], 'nextCursor': 'repeat'}):
            with self.assertRaisesRegex(AppServerError, 'invalid cursor'):
                adapter.list_models()
        with patch.object(adapter, 'list_models', side_effect=AppServerError('catalog unavailable')):
            self.assertEqual(self.request('GET', '/api/workspace/models')[0], 503)
            self.assertEqual(self.create(self.payload())[0], 503)
        self.assertEqual(len(self.server.project_registry.contexts()), 1)
        with patch.object(self.root_context.gateway, 'adapter', None):
            self.assertFalse(self.root_context.gateway.state()['project_creation_supported'])
            self.assertEqual(self.create(self.payload())[0], 409)

    def test_invalid_model_effort_and_permission_create_nothing(self):
        for key, value in [('model', 'text-only'), ('reasoning_effort', 'invalid'), ('permission_mode', 'invalid')]:
            payload = self.payload(); payload[key] = value
            self.assertEqual(self.create(payload)[0], 400)
        self.assertEqual(len(self.server.project_registry.contexts()), 1)
        self.assertEqual(len(self.wire('thread/start')), 1)

    def test_permission_modes_and_failed_creation_never_replayed(self):
        for permission, sandbox, approval in [('read_only', 'read-only', 'on-request'), ('full_access', 'danger-full-access', 'never')]:
            payload = self.payload(); payload['permission_mode'] = permission
            status, result, _ = self.create(payload)
            self.assertEqual(status, 201, result)
            start = self.wire('thread/start')[-1]
            self.assertEqual((start['sandbox'], start['approvalPolicy']), (sandbox, approval))
        payload = self.payload()
        with patch.object(CodexAppServerAdapter, 'start', side_effect=RuntimeError('fixture unavailable')):
            # Root model catalog is already read in this fixture; fail only child startup.
            with patch.object(self.root_context.gateway, 'list_models', return_value={'models':[{'model':'fixture-vision','supported_reasoning_efforts':['high']}]}):
                status, result, _ = self.create(payload)
                self.assertEqual(status, 503)
                self.assertEqual(self.create(payload)[0], 503)
        starts = len(self.wire('thread/start'))
        self.stop_server(); self.start_server()
        self.assertEqual(self.create(payload)[0], 503)
        self.assertEqual(len(self.wire('thread/start')), starts)

if __name__ == '__main__':
    unittest.main()
