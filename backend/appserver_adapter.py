"""A small, persistent JSON-RPC client for ``codex app-server --stdio``.

The wire shapes in this module were checked against ``codex-cli 0.156.1`` with
``codex app-server generate-json-schema``.  The adapter owns one non-ephemeral
Codex thread for a project.  Callbacks run on a separate dispatcher thread, so
they may safely call back into the adapter.

This layer only transports user turns and server events.  A Gateway owns its
durable feedback queue and decides when to start the next turn.  In particular,
an uncertain ``turn/start`` response is NEVER replayed automatically.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Sequence


EventCallback = Callable[[dict[str, Any]], None]
SUPPORTED_CLI_VERSION = "0.156.1"


class AppServerError(RuntimeError):
    """An app-server RPC or transport operation failed."""


class TurnBusyError(AppServerError):
    """Another turn is active or its outcome has not been reconciled."""


class UncertainDeliveryError(AppServerError):
    """The turn request may have reached Codex; never retry it blindly."""


class PendingRequestNotFound(AppServerError):
    """The approval or input request was already resolved or lost."""


class _PendingRPC:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: dict[str, Any] | None = None
        self.error: Exception | None = None


class CodexAppServerAdapter:
    """Thread-safe stdio transport for one project-owned Codex conversation.

    ``start()`` initializes the process, then creates or resumes the thread.
    ``start_turn()`` sends a text item followed by actual local image items.
    Events are delivered to ``on_event`` as ``{"method": ..., "params": ...}``.
    App-server notifications keep their original method names.  Additional
    adapter events are ``adapter/request_pending``, ``adapter/disconnected``,
    and ``adapter/reconnected``.

    The caller must present server-initiated requests to a human and call
    ``respond_to_request`` with the chosen result.  The adapter never approves
    commands, file changes, permissions, or MCP requests on its own.
    """

    def __init__(
        self,
        project_dir: str | os.PathLike[str],
        state_path: str | os.PathLike[str] | None = None,
        on_event: EventCallback | None = None,
        command: Sequence[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str = "on-request",
        sandbox: str = "workspace-write",
        network_access: bool = True,
        request_timeout: float = 30.0,
        verify_version: bool = True,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve(strict=True)
        if not self.project_dir.is_dir():
            raise ValueError("project_dir must be a directory")
        self.state_path = (
            Path(state_path).expanduser().resolve()
            if state_path is not None
            else self.project_dir / "backend" / "data" / "codex_app_server_thread.json"
        )
        self.on_event = on_event
        self.command = list(command or ("codex", "app-server", "--stdio"))
        self.model = model
        self.reasoning_effort = reasoning_effort
        if approval_policy not in {"on-request", "untrusted", "never"}:
            raise ValueError("invalid approval_policy")
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("invalid sandbox")
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self.network_access = network_access
        self.request_timeout = request_timeout
        self.verify_version = verify_version
        self.env_overrides = dict(env_overrides or {})
        self._thread_config = self._project_mcp_config()

        self._lifecycle_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._turn_lock = threading.Lock()
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._dispatcher = threading.Thread(target=self._dispatch_events, daemon=True, name="codex-events")
        self._dispatcher.start()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._pending_rpc: dict[str, _PendingRPC] = {}
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self._generation = 0
        self._closed = False
        self._connected = False
        self._thread_id: str | None = None
        self._active_turn_id: str | None = None
        self._turn_state = "idle"
        self._last_turn: dict[str, Any] | None = None
        # Unknown legacy state is treated conservatively as a possible turn.
        self._has_turn_attempt = True

    @property
    def thread_id(self) -> str | None:
        with self._state_lock:
            return self._thread_id

    def configure_model(self, model: str, reasoning_effort: str | None, *, persist=None) -> None:
        """Change future turns without replacing the thread or its permissions.

        The durable commit runs while turn submission is excluded; failed saves
        leave the running adapter unchanged. Reconnects use the same settings.
        """
        with self._turn_lock, self._state_lock:
            if self._turn_state != "idle":
                raise TurnBusyError("finish or resolve the current turn before changing models")
            if persist is not None:
                persist()
            self.model = model
            self.reasoning_effort = reasoning_effort
            self._thread_config.pop("model_reasoning_effort", None)
            if reasoning_effort is not None:
                self._thread_config["model_reasoning_effort"] = reasoning_effort

    def start(self) -> str:
        """Start or reconnect stdio and return this project's persisted thread ID.

        A stored thread that cannot be resumed is an error; silently creating a
        replacement would break the user's same-conversation guarantee.
        """
        with self._lifecycle_lock:
            with self._state_lock:
                if self._closed:
                    raise AppServerError("adapter is closed")
                if self._connected and self._process and self._process.poll() is None and self._thread_id:
                    return self._thread_id
            if self.verify_version:
                self._check_cli_version()
            self._spawn()
            try:
                self._rpc(
                    "initialize",
                    {"clientInfo": {"name": "scene_feedback_workspace", "title": "Scene Feedback Workspace", "version": "0.2.0"}},
                )
                self._send({"method": "initialized", "params": {}})
                # Keep the known ID in memory if persisting it failed in a
                # previous attempt.  Never silently start a different thread.
                stored = self._load_thread_state()
                stored_id = (stored or {}).get("thread_id") or self._thread_id
                if stored is not None:
                    self._has_turn_attempt = stored["has_turn_attempt"]
                replaced_empty_id: str | None = None
                if stored_id:
                    try:
                        result = self._rpc(
                            "thread/resume",
                            {
                                "threadId": stored_id,
                                "cwd": str(self.project_dir),
                                "model": self.model,
                                "approvalPolicy": self.approval_policy,
                                "approvalsReviewer": "user",
                                "sandbox": self.sandbox,
                                "config": self._thread_config,
                            },
                        )
                    except _RPCRejected as exc:
                        # App Server 0.156.1 can return a thread/start ID
                        # before any rollout exists.  A zero-turn thread has
                        # no conversation to lose; record its replacement.
                        if self._has_turn_attempt or "no rollout found" not in str(exc).lower():
                            raise
                        replaced_empty_id = stored_id
                        stored_id = None
                if not stored_id:
                    params: dict[str, Any] = {
                        "cwd": str(self.project_dir),
                        "ephemeral": False,
                        "approvalPolicy": self.approval_policy,
                        "approvalsReviewer": "user",
                        "sandbox": self.sandbox,
                        "serviceName": "scene_feedback_workspace",
                        "config": self._thread_config,
                    }
                    if self.model:
                        params["model"] = self.model
                    result = self._rpc("thread/start", params)
                thread = result.get("thread")
                if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                    raise AppServerError("thread/start or resume returned no thread ID")
                if thread.get("ephemeral") is True:
                    raise AppServerError("app-server returned an ephemeral thread")
                thread_id = thread["id"]
                if stored_id and thread_id != stored_id:
                    raise AppServerError("app-server resumed a different thread")
                with self._state_lock:
                    self._thread_id = thread_id
                    self._reconcile_thread(thread)
                if not stored_id:
                    self._save_thread_state(thread_id, has_turn_attempt=False)
                    self._has_turn_attempt = False
                self._verify_permissions(result)
                if replaced_empty_id:
                    self._emit(
                        "adapter/empty_thread_recreated",
                        {"old_thread_id": replaced_empty_id, "thread_id": thread_id},
                    )
                self._emit(
                    "adapter/reconnected",
                    {"thread_id": thread_id, "resumed": bool(stored_id), "status": self.status()},
                )
                return thread_id
            except Exception:
                self._disconnect(self._generation, "initialization or thread resume failed", terminate=True)
                raise

    def _verify_permissions(self, result: dict[str, Any]) -> None:
        """Reject an authoritative response that ignored configured access.

        This isolated transport keeps the legacy workspace network setting.
        A Desktop named profile must never silently replace that contract.
        Older servers omit effective metadata; explicit turn overrides still
        carry the configured policy for those servers.
        """
        expected_type = {
            "read-only": "readOnly",
            "workspace-write": "workspaceWrite",
            "danger-full-access": "dangerFullAccess",
        }[self.sandbox]
        if "sandbox" in result:
            policy = result["sandbox"]
            if not isinstance(policy, dict) or policy.get("type") != expected_type:
                raise AppServerError("Codex did not apply the configured sandbox permissions")
            if self.sandbox == "workspace-write" and "networkAccess" in policy:
                if policy["networkAccess"] != self.network_access:
                    raise AppServerError("Codex did not apply the configured workspace network access")
        if "approvalPolicy" in result and result["approvalPolicy"] != self.approval_policy:
            raise AppServerError("Codex did not apply the configured approval policy")
        if "approvalsReviewer" in result and result["approvalsReviewer"] != "user":
            raise AppServerError("Codex did not apply the configured approvals reviewer")
        profile = result.get("activePermissionProfile")
        if profile is not None:
            expected_profile = {
                "read-only": ":read-only",
                "workspace-write": ":workspace",
                "danger-full-access": ":danger-full-access",
            }[self.sandbox]
            if not isinstance(profile, dict) or profile.get("id") != expected_profile:
                raise AppServerError("Codex selected a different permission profile")

    def list_models(self) -> list[dict[str, Any]]:
        """Read the account's image-model catalog without sending a user turn."""
        self.start()
        entries: list[dict[str, Any]] = []
        cursor = None
        seen = set()
        for _ in range(10):
            params = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._rpc("model/list", params)
            page = response.get("data")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise AppServerError("model/list returned invalid models")
            entries.extend(page)
            cursor = response.get("nextCursor")
            if cursor is None:
                return entries
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise AppServerError("model/list returned an invalid cursor")
            seen.add(cursor)
        raise AppServerError("too many Codex models to list safely")

    def start_turn(
        self,
        text: str,
        image_paths: Sequence[str | os.PathLike[str]] = (),
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit one user message and return ``{thread_id, turn_id, status}``.

        Paths are resolved and checked before the RPC; image bytes remain on
        disk, and App Server receives each as a ``localImage`` input item.
        ``message_id`` is a caller-owned idempotency identifier recorded on the
        user message.  A lost response still requires reconciliation, because
        the server does not promise deduplication of repeated ``turn/start``.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be nonempty")
        if message_id is not None and (not isinstance(message_id, str) or not message_id):
            raise ValueError("message_id must be nonempty text")
        images: list[str] = []
        for path in image_paths:
            resolved = Path(path).expanduser().resolve(strict=True)
            if not resolved.is_file():
                raise ValueError(f"image is not a file: {resolved}")
            images.append(str(resolved))
        with self._turn_lock:
            thread_id = self.start()
            with self._state_lock:
                if self._turn_state != "idle":
                    raise TurnBusyError(f"turn state is {self._turn_state}; wait or reconcile before submitting")
                if not self._has_turn_attempt:
                    # Persist this BEFORE sending turn/start.  A disconnected
                    # RPC might have reached Codex, so its thread can no
                    # longer be safely replaced without reconciliation.
                    self._save_thread_state(thread_id, has_turn_attempt=True)
                    self._has_turn_attempt = True
                self._turn_state = "starting"
            items = [{"type": "text", "text": text}]
            items.extend({"type": "localImage", "path": path} for path in images)
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": items,
                "approvalPolicy": self.approval_policy,
                "approvalsReviewer": "user",
                "sandboxPolicy": {
                    "read-only": {"type": "readOnly"},
                    "workspace-write": {"type": "workspaceWrite", "networkAccess": self.network_access},
                    "danger-full-access": {"type": "dangerFullAccess"},
                }[self.sandbox],
            }
            if self.model:
                params["model"] = self.model
            # Explicit null clears an earlier turn's effort override.
            params["effort"] = self.reasoning_effort
            if message_id is not None:
                params["clientUserMessageId"] = message_id
            try:
                result = self._rpc("turn/start", params)
            except AppServerError as exc:
                with self._state_lock:
                    # A JSON-RPC error is authoritative; a timeout or closed
                    # transport is not.  Never replay uncertain input here.
                    if isinstance(exc, _RPCRejected):
                        self._turn_state = "idle"
                    else:
                        self._turn_state = "unknown"
                if isinstance(exc, _RPCRejected):
                    raise
                raise UncertainDeliveryError(str(exc)) from exc
            turn = result.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                with self._state_lock:
                    self._turn_state = "unknown"
                raise UncertainDeliveryError("turn/start returned no turn ID")
            with self._state_lock:
                # A very short turn can complete before this RPC response is
                # handled.  Keep the authoritative completion notification.
                if not (self._last_turn and self._last_turn.get("id") == turn["id"] and
                        self._last_turn.get("status") != "inProgress"):
                    self._active_turn_id = turn["id"] if turn.get("status") == "inProgress" else None
                    self._turn_state = "active" if turn.get("status") == "inProgress" else "idle"
                    self._last_turn = turn
            return {"thread_id": thread_id, "turn_id": turn["id"], "status": turn.get("status", "inProgress")}

    def interrupt(self, turn_id: str | None = None) -> dict[str, Any]:
        """Request cancellation of the active turn; completion arrives as an event."""
        thread_id = self.start()
        with self._state_lock:
            target = turn_id or self._active_turn_id
        if not target:
            raise AppServerError("no active turn ID to interrupt")
        result = self._rpc("turn/interrupt", {"threadId": thread_id, "turnId": target})
        return {"thread_id": thread_id, "turn_id": target, "result": result}

    def respond_to_request(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Return one explicit human response to a pending app-server request."""
        key = str(request_id)
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        with self._state_lock:
            request = self._pending_requests.get(key)
        if request is None:
            raise PendingRequestNotFound(key)
        self._validate_server_response(request["method"], result)
        # Preserve the wire id's original numeric/string type.
        self._send({"id": request["id"], "result": result})
        with self._state_lock:
            self._pending_requests.pop(key, None)

    def refresh(self) -> dict[str, Any]:
        """Read authoritative thread state after a reconnect or uncertain RPC."""
        thread_id = self.start()
        result = self._rpc("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError("thread/read returned no thread")
        with self._state_lock:
            self._reconcile_thread(thread)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "connected": self._connected,
                "thread_id": self._thread_id,
                "turn_id": self._active_turn_id,
                "turn_state": self._turn_state,
                "last_turn": self._last_turn,
                "pending_requests": list(self._pending_requests.values()),
            }

    def close(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if self._closed:
                    return
                self._closed = True
                generation = self._generation
            self._disconnect(generation, "adapter closed", terminate=True)
            self._events.put(None)
        if threading.current_thread() is not self._dispatcher:
            self._dispatcher.join(timeout=2)

    def _spawn(self) -> None:
        if not shutil.which(self.command[0]) and not Path(self.command[0]).exists():
            raise AppServerError(f"command not found: {self.command[0]}")
        process = subprocess.Popen(
            self.command,
            cwd=self.project_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, **self.env_overrides},
        )
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            self._process = process
            self._connected = True
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(process, generation),
            daemon=True,
            name="codex-stdio-reader",
        )
        self._reader.start()

    def _project_mcp_config(self) -> dict[str, Any]:
        """Bind this Codex thread's MCP tools to this exact Gateway instance.

        Child MCP processes are configured by the App Server thread config,
        not merely by environment variables on the App Server process.  The
        latter may be absent when a stored thread is resumed by another host.
        """
        harness_root = Path(__file__).resolve().parent.parent
        mcp_script = Path(__file__).resolve().with_name("mcp_server.py")
        mcp_env = {
            "SCENE_FEEDBACK_PORT": self.env_overrides.get("SCENE_FEEDBACK_PORT", "18765"),
            "SCENE_FEEDBACK_DATA_DIR": self.env_overrides.get(
                "SCENE_FEEDBACK_DATA_DIR", str(self.state_path.parent)
            ),
            "SCENE_FEEDBACK_PROJECT_DIR": str(self.project_dir),
            "SCENE_FEEDBACK_WEB_DIR": self.env_overrides.get(
                "SCENE_FEEDBACK_WEB_DIR", str(harness_root / "web")
            ),
        }
        return {
            **({"sandbox_workspace_write.network_access": self.network_access}
               if self.sandbox == "workspace-write" else {}),
            **({"model_reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}),
            "mcp_servers": {
                "scene_feedback": {
                    "command": sys.executable,
                    "args": [str(mcp_script)],
                    "env": mcp_env,
                    "tool_timeout_sec": 360,
                    "required": True,
                }
            }
        }

    def _check_cli_version(self) -> None:
        try:
            completed = subprocess.run(
                [self.command[0], "--version"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppServerError(f"cannot verify codex-cli version: {exc}") from exc
        reported = completed.stdout.strip()
        expected = f"codex-cli {SUPPORTED_CLI_VERSION}"
        if reported != expected:
            raise AppServerError(
                f"App Server protocol was verified with {expected}; found {reported!r}. "
                "Regenerate the JSON schema and recheck the adapter before running "
                "a different Codex version."
            )

    def _read_loop(self, process: subprocess.Popen[str], generation: int) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._emit("adapter/protocol_error", {"error": "non-JSON stdout from app-server"})
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    with self._state_lock:
                        pending = self._pending_rpc.pop(str(message["id"]), None)
                    if pending:
                        pending.response = message
                        pending.done.set()
                elif "id" in message and "method" in message:
                    with self._state_lock:
                        self._pending_requests[str(message["id"])] = {
                            "id": message["id"],
                            "method": message["method"],
                            "params": message.get("params", {}),
                        }
                    self._emit("adapter/request_pending", {
                        "request_id": message["id"],
                        "method": message["method"],
                        "params": message.get("params", {}),
                    })
                elif "method" in message:
                    self._handle_notification(message)
        finally:
            self._disconnect(generation, "app-server stdio closed", terminate=False)

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        with self._state_lock:
            if method == "turn/started" and params.get("threadId") == self._thread_id:
                turn = params.get("turn") or {}
                self._active_turn_id = turn.get("id") or self._active_turn_id
                self._turn_state = "active"
                self._last_turn = turn
            elif method == "turn/completed" and params.get("threadId") == self._thread_id:
                turn = params.get("turn") or {}
                self._last_turn = turn
                if turn.get("id") == self._active_turn_id:
                    self._active_turn_id = None
                    self._turn_state = "idle"
            elif method == "serverRequest/resolved":
                request_id = params.get("requestId")
                if request_id is not None:
                    self._pending_requests.pop(str(request_id), None)
        self._events.put(message)

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        pending = _PendingRPC()
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending_rpc[str(request_id)] = pending
        try:
            self._send({"method": method, "id": request_id, "params": params})
        except Exception:
            with self._state_lock:
                self._pending_rpc.pop(str(request_id), None)
            raise
        if not pending.done.wait(self.request_timeout):
            with self._state_lock:
                self._pending_rpc.pop(str(request_id), None)
            raise AppServerError(f"{method} timed out after {self.request_timeout:g}s")
        if pending.error:
            raise AppServerError(f"{method}: {pending.error}") from pending.error
        response = pending.response or {}
        if "error" in response:
            error = response["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise _RPCRejected(f"{method}: {message}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise AppServerError(f"{method} returned an invalid result")
        return result

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._send_lock:
            with self._state_lock:
                process = self._process
                connected = self._connected
            if not connected or process is None or process.stdin is None or process.poll() is not None:
                raise AppServerError("app-server is disconnected")
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as exc:
                self._disconnect(self._generation, f"app-server write failed: {exc}", terminate=True)
                raise AppServerError("app-server write failed") from exc

    def _disconnect(self, generation: int, reason: str, *, terminate: bool) -> None:
        with self._state_lock:
            if generation != self._generation or not self._connected:
                return
            self._connected = False
            process = self._process
            self._process = None
            pending = list(self._pending_rpc.values())
            self._pending_rpc.clear()
            pending_requests = list(self._pending_requests.values())
            self._pending_requests.clear()
            if self._turn_state != "idle":
                self._turn_state = "unknown"
        for call in pending:
            call.error = AppServerError(reason)
            call.done.set()
        if terminate and process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process:
            if process.stdin:
                process.stdin.close()
            if self._reader and self._reader is not threading.current_thread():
                self._reader.join(timeout=2)
            if process.stdout:
                process.stdout.close()
        self._emit(
            "adapter/disconnected",
            {"reason": reason, "turn_id": self._active_turn_id, "lost_requests": pending_requests},
        )

    def _reconcile_thread(self, thread: dict[str, Any]) -> None:
        turns = thread.get("turns") or []
        if isinstance(turns, list) and turns:
            last = turns[-1]
            if isinstance(last, dict):
                self._last_turn = last
                if last.get("status") == "inProgress":
                    self._active_turn_id = last.get("id")
                    self._turn_state = "active"
                    return
        status = thread.get("status") or {}
        if isinstance(status, dict) and status.get("type") == "active":
            # A turn may be live even when an abbreviated resume omits its id.
            self._turn_state = "unknown"
        else:
            self._active_turn_id = None
            self._turn_state = "idle"

    def _load_thread_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppServerError(f"cannot read stored thread state: {exc}") from exc
        if state.get("project_dir") != str(self.project_dir):
            raise AppServerError("stored thread belongs to another project directory")
        thread_id = state.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("stored thread ID is invalid")
        attempted = state.get("has_turn_attempt", True)
        if not isinstance(attempted, bool):
            raise AppServerError("stored turn-attempt flag is invalid")
        return {"thread_id": thread_id, "has_turn_attempt": attempted}

    def _save_thread_state(self, thread_id: str, *, has_turn_attempt: bool) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "project_dir": str(self.project_dir),
            "thread_id": thread_id,
            "has_turn_attempt": has_turn_attempt,
        }, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".codex-thread-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _validate_server_response(method: str, result: dict[str, Any]) -> None:
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            allowed = {"accept", "acceptForSession", "decline", "cancel"}
            decision = result.get("decision")
            approved_amendment = (
                method == "item/commandExecution/requestApproval"
                and isinstance(decision, dict)
                and isinstance(decision.get("acceptWithExecpolicyAmendment"), dict)
            )
            if not ((isinstance(decision, str) and decision in allowed) or approved_amendment):
                raise ValueError("invalid approval decision")
        elif method == "item/permissions/requestApproval":
            if not isinstance(result.get("permissions"), dict):
                raise ValueError("permission response requires permissions object")
        elif method == "mcpServer/elicitation/request":
            if result.get("action") not in {"accept", "decline", "cancel"}:
                raise ValueError("invalid elicitation action")
        elif method == "item/tool/requestUserInput":
            if not isinstance(result.get("answers"), dict):
                raise ValueError("user input response requires answers object")

    def _emit(self, method: str, params: dict[str, Any]) -> None:
        self._events.put({"method": method, "params": params})

    def _dispatch_events(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            if self.on_event:
                try:
                    self.on_event(event)
                except Exception:
                    # A UI callback must not kill the transport reader or
                    # silently approve/alter agent work.  Keep the error
                    # visible in the Gateway process log.
                    logging.exception("Codex App Server event callback failed")


class _RPCRejected(AppServerError):
    """The app-server returned a definite JSON-RPC error."""
