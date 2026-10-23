"""Transport checks with a tiny app-server protocol fixture.

The separate real-app-server smoke is run manually because it requires Codex
login and a model request.  These tests catch the important local behavior:
actual localImage wire items, persistent resume, turn events, and human-only
approval responses.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appserver_adapter import AppServerError, CodexAppServerAdapter, TurnBusyError  # noqa: E402


FAKE_SERVER = r'''
import json, sys
from pathlib import Path
log = Path(sys.argv[1])
permission_metadata = sys.argv[2] if len(sys.argv) > 2 else None
turn_number = 0
def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()
def thread_result(thread_id):
    result = {"thread": {"id": thread_id, "ephemeral": False, "status": {"type": "idle"}, "turns": []}}
    if permission_metadata == "read-only":
        result.update(sandbox={"type": "readOnly", "networkAccess": False}, approvalPolicy="never",
                      activePermissionProfile={"id": ":read-only", "extends": None})
    elif permission_metadata == "workspace-no-network":
        result.update(sandbox={"type": "workspaceWrite", "networkAccess": False}, approvalPolicy="on-request")
    elif permission_metadata == "configured-network":
        result.update(sandbox={"type": "workspaceWrite", "networkAccess": msg["params"].get("config", {}).get("sandbox_workspace_write.network_access", False)}, approvalPolicy="on-request")
    elif permission_metadata == "external":
        result.update(sandbox={"type": "externalSandbox"}, approvalPolicy="never")
    return result
for line in sys.stdin:
    msg = json.loads(line)
    with log.open("a") as handle:
        handle.write(json.dumps(msg) + "\n")
    method = msg.get("method")
    request_id = msg.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        send({"id": request_id, "result": thread_result("thread-test")})
    elif method == "thread/resume":
        if msg["params"]["threadId"] == "lost-empty":
            send({"id": request_id, "error": {"code": -32000, "message": "no rollout found for thread id lost-empty"}})
        else:
            send({"id": request_id, "result": thread_result(msg["params"]["threadId"])})
    elif method == "thread/read":
        send({"id": request_id, "result": {"thread": {"id": "thread-test", "status": {"type": "idle"}, "turns": []}}})
    elif method == "turn/start":
        turn_number += 1
        turn = {"id": f"turn-{turn_number}", "status": "inProgress", "items": []}
        send({"id": request_id, "result": {"turn": turn}})
        send({"method": "turn/started", "params": {"threadId": "thread-test", "turn": turn}})
        if "approval" in msg["params"]["input"][0]["text"]:
            send({"id": 900, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-test", "turnId": turn["id"], "command": "echo checked"}})
        else:
            send({"method": "turn/completed", "params": {"threadId": "thread-test", "turn": {"id": turn["id"], "status": "completed", "items": []}}})
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
        send({"method": "turn/completed", "params": {"threadId": "thread-test", "turn": {"id": msg["params"]["turnId"], "status": "interrupted", "items": []}}})
    elif request_id == 900 and "result" in msg:
        send({"method": "serverRequest/resolved", "params": {"threadId": "thread-test", "requestId": 900}})
        send({"method": "turn/completed", "params": {"threadId": "thread-test", "turn": {"id": f"turn-{turn_number}", "status": "completed", "items": []}}})
'''


def until(predicate, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for app-server event")


class AppServerAdapterTests(unittest.TestCase):
    def fixture(self, directory: str, events: list[dict], **options):
        root = Path(directory)
        script = root / "fake_app_server.py"
        script.write_text(FAKE_SERVER)
        log = root / "wire.jsonl"
        permission_metadata = options.pop("permission_metadata", "")
        adapter = CodexAppServerAdapter(
            root,
            state_path=root / "thread.json",
            command=[sys.executable, "-u", str(script), str(log), permission_metadata],
            env_overrides={
                "SCENE_FEEDBACK_PORT": "19876",
                "SCENE_FEEDBACK_DATA_DIR": str(root / "isolated-data"),
                "SCENE_FEEDBACK_PROJECT_DIR": str(root),
            },
            verify_version=False,
            on_event=events.append,
            request_timeout=2,
            **options,
        )
        return adapter, log

    def test_local_images_and_same_thread_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events: list[dict] = []
            adapter, log = self.fixture(directory, events)
            image = Path(directory) / "reference.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            try:
                self.assertEqual(adapter.start(), "thread-test")
                self.assertEqual(adapter.start_turn("round one", [image], "feedback-1")["thread_id"], "thread-test")
                until(lambda: adapter.status()["turn_state"] == "idle")
            finally:
                adapter.close()
            resumed, _ = self.fixture(directory, events)
            try:
                self.assertEqual(resumed.start(), "thread-test")
                self.assertEqual(resumed.start_turn("round two", [image], "feedback-2")["thread_id"], "thread-test")
                until(lambda: resumed.status()["turn_state"] == "idle")
            finally:
                resumed.close()
            frames = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len([frame for frame in frames if frame.get("method") == "thread/start"]), 1)
            self.assertEqual(len([frame for frame in frames if frame.get("method") == "thread/resume"]), 1)
            for frame in frames:
                if frame.get("method") not in {"thread/start", "thread/resume"}:
                    continue
                mcp = frame["params"]["config"]["mcp_servers"]["scene_feedback"]
                self.assertEqual(mcp["command"], sys.executable)
                self.assertEqual(mcp["args"], [str(Path(__file__).resolve().parents[1] / "mcp_server.py")])
                self.assertEqual(mcp["env"]["SCENE_FEEDBACK_PORT"], "19876")
                self.assertEqual(mcp["env"]["SCENE_FEEDBACK_DATA_DIR"], str(Path(directory) / "isolated-data"))
                self.assertEqual(mcp["env"]["SCENE_FEEDBACK_PROJECT_DIR"], str(Path(directory)))
            turns = [frame for frame in frames if frame.get("method") == "turn/start"]
            self.assertEqual([frame["params"]["clientUserMessageId"] for frame in turns], ["feedback-1", "feedback-2"])
            self.assertEqual(turns[0]["params"]["input"][1], {"type": "localImage", "path": str(image)})
            self.assertEqual(turns[0]["params"]["sandboxPolicy"], {"type": "workspaceWrite", "networkAccess": True})
            self.assertTrue(any(event["method"] == "turn/completed" for event in events))

    def test_approval_waits_for_explicit_human_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events: list[dict] = []
            adapter, log = self.fixture(directory, events)
            try:
                adapter.start_turn("approval please")
                until(lambda: bool(adapter.status()["pending_requests"]))
                self.assertEqual(adapter.status()["turn_state"], "active")
                with self.assertRaises(TurnBusyError):
                    adapter.start_turn("premature second turn")
                before = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(frame.get("id") == 900 for frame in before))
                adapter.respond_to_request(900, {"decision": "decline"})
                until(lambda: adapter.status()["turn_state"] == "idle")
                after = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual([frame for frame in after if frame.get("id") == 900][0]["result"], {"decision": "decline"})
            finally:
                adapter.close()

    def test_selected_permissions_survive_process_restart_and_later_turns(self) -> None:
        for sandbox, approval, effective in (
            ("danger-full-access", "never", {"type": "dangerFullAccess"}),
            ("workspace-write", "on-request", {"type": "workspaceWrite", "networkAccess": True}),
            ("read-only", "on-request", {"type": "readOnly"}),
        ):
            with self.subTest(sandbox=sandbox), tempfile.TemporaryDirectory() as directory:
                for round_number in (1, 2):
                    adapter, log = self.fixture(directory, [], sandbox=sandbox, approval_policy=approval)
                    try:
                        self.assertEqual(adapter.start(), "thread-test")
                        adapter.start_turn(f"round {round_number}", message_id=f"feedback-{round_number}")
                        until(lambda: adapter.status()["turn_state"] == "idle")
                    finally:
                        adapter.close()
                frames = [json.loads(line) for line in log.read_text().splitlines()]
                starts = [item for item in frames if item.get("method") in {"thread/start", "thread/resume"}]
                self.assertEqual([item["method"] for item in starts], ["thread/start", "thread/resume"])
                for frame in starts:
                    self.assertEqual(frame["params"]["sandbox"], sandbox)
                    self.assertEqual(frame["params"]["approvalPolicy"], approval)
                turns = [item for item in frames if item.get("method") == "turn/start"]
                self.assertEqual(len(turns), 2)
                for frame in turns:
                    self.assertEqual(frame["params"]["sandboxPolicy"], effective)
                    self.assertEqual(frame["params"]["approvalPolicy"], approval)

    def test_workspace_network_is_applied_before_start_and_resume_verification(self) -> None:
        for network in (True, False):
            with self.subTest(network=network), tempfile.TemporaryDirectory() as directory:
                for _ in range(2):
                    adapter, log = self.fixture(directory, [], network_access=network,
                                                permission_metadata="configured-network")
                    try:
                        self.assertEqual(adapter.start(), "thread-test")
                    finally:
                        adapter.close()
                frames = [json.loads(line) for line in log.read_text().splitlines()]
                methods = [frame['method'] for frame in frames if frame.get('method') in {'thread/start', 'thread/resume', 'turn/start'}]
                self.assertEqual(methods, ['thread/start', 'thread/resume'])

    def test_effective_read_only_override_is_rejected_without_losing_created_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for attempt in range(2):
                adapter, log = self.fixture(directory, [], sandbox="danger-full-access",
                                            approval_policy="never", permission_metadata="read-only")
                try:
                    with self.assertRaisesRegex(AppServerError, "configured sandbox permissions"):
                        adapter.start_turn("must not be delivered")
                    self.assertEqual(json.loads((Path(directory) / "thread.json").read_text())["thread_id"], "thread-test")
                finally:
                    adapter.close()
            frames = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([frame["method"] for frame in frames if frame.get("method") in {"thread/start", "thread/resume"}],
                             ["thread/start", "thread/resume"])
            self.assertFalse(any(frame.get("method") == "turn/start" for frame in frames))

    def test_explicit_network_and_external_policy_mismatches_are_rejected(self) -> None:
        for mode, sandbox, approval, error in (
            ("workspace-no-network", "workspace-write", "on-request", "workspace network access"),
            ("external", "danger-full-access", "never", "sandbox permissions"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                adapter, log = self.fixture(directory, [], sandbox=sandbox, approval_policy=approval,
                                            permission_metadata=mode)
                try:
                    with self.assertRaisesRegex(AppServerError, error):
                        adapter.start_turn("must not be delivered")
                    frames = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertFalse(any(frame.get("method") == "turn/start" for frame in frames))
                finally:
                    adapter.close()

    def test_missing_empty_thread_is_recorded_and_recreated_only_before_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "thread.json"
            state.write_text(json.dumps({
                "project_dir": str(root), "thread_id": "lost-empty", "has_turn_attempt": False,
            }))
            events: list[dict] = []
            adapter, log = self.fixture(directory, events)
            try:
                self.assertEqual(adapter.start(), "thread-test")
                until(lambda: any(event["method"] == "adapter/empty_thread_recreated" for event in events))
                self.assertEqual(json.loads(state.read_text())["thread_id"], "thread-test")
                frame = next(json.loads(line) for line in log.read_text().splitlines() if '"method": "thread/start"' in line)
                self.assertEqual(frame["params"]["config"]["mcp_servers"]["scene_feedback"]["tool_timeout_sec"], 360)
            finally:
                adapter.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "thread.json").write_text(json.dumps({
                "project_dir": str(root), "thread_id": "lost-empty", "has_turn_attempt": True,
            }))
            adapter, log = self.fixture(directory, [])
            try:
                with self.assertRaisesRegex(AppServerError, "no rollout found"):
                    adapter.start()
                frames = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(frame.get("method") == "thread/start" for frame in frames))
            finally:
                adapter.close()


if __name__ == "__main__":
    unittest.main()
