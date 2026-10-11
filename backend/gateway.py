"""Durable browser-to-Codex delivery for one local reconstruction workspace."""

from __future__ import annotations

import copy
import base64
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from appserver_adapter import TurnBusyError
from core import APIError, MAX_REFERENCE_BYTES, MAX_REFERENCES, SceneStore, _now
from feedback_summary import model_input_plan, image_caption
from shared_thread_adapter import DeliveryNotReadyError, DeliveryRejectedError
from shared_thread_adapter import SharedDesktopAdapter
from shared_thread_bridge import OwnedEmptyThreadMissing, SharedThreadBridge, SharedThreadNotIdle
from shared_thread_bridge import SharedThreadBridgeError, SharedThreadRPCRejected, SharedThreadPermissionMismatch


class WorkspaceGateway:
    def __init__(self, store: SceneStore, project_dir: str | Path, *, adapter: Any = None, external_review: bool = False,
                 desktop_seed_thread_id: str | None = None, thread_config: dict[str, Any] | None = None,
                 target_validator: Any = None, feedback_transport: str = "legacy"):
        if feedback_transport not in {"legacy", "mcp_events"}:
            raise ValueError("unknown feedback transport")
        if feedback_transport == "mcp_events" and (adapter is not None or not external_review):
            raise ValueError("MCP events require external review without a Codex adapter")
        self.feedback_transport = feedback_transport
        self.service_feedback_transport = feedback_transport
        self.mcp_events: Any = None
        self.store = store
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.adapter = adapter
        self.external_review = external_review
        self.desktop_seed_thread_id = desktop_seed_thread_id
        self.thread_config = copy.deepcopy(thread_config)
        self.target_validator = target_validator
        self.target_binding_lock = threading.RLock()
        self.project_name: str | None = None
        self.registry_project_id: str | None = None
        self.pose_jobs: Any = None
        self._worker_lock = threading.Lock()
        self._worker_running = False
        self._worker_thread: threading.Thread | None = None
        self._started = False
        self._supervisor_lock = threading.Lock()
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._adapter_initialized = False
        self._adapter_event_lock = threading.RLock()
        self._adapter_token = object()
        self._route_lock = threading.RLock()

    _RECONCILE_INTERVAL_SEC = 5.0
    _UNCERTAIN_GRACE_SEC = 30.0

    def ensure(self, preferred_session_id: str | None = None) -> dict[str, Any]:
        self.store.ensure_workspace(self.project_dir, preferred_session_id=preferred_session_id, project_id=self.registry_project_id)
        mode = "external" if self.external_review else "appserver"
        with self.store.lock:
            stored = self.store.state["workspace"]
            transport = stored.get("feedback_transport", "legacy")
            existing = stored.get("delivery_mode")
            if existing is not None and existing != mode:
                raise APIError(409, f"workspace is already bound to {existing} delivery")
            explicitly_bound = (self.external_review and transport == "legacy"
                                and stored.get("task_connection_mode") == "desktop"
                                and stored.get("feedback_transport_origin") == "mcp_events"
                                and bool(stored.get("thread_id")))
            if transport != self.service_feedback_transport:
                if self.service_feedback_transport == "mcp_events" and explicitly_bound:
                    self.feedback_transport = "legacy"
                elif stored.get("thread_id") or stored.get("queue") or "feedback_transport" in stored:
                    raise APIError(409, "workspace feedback transport differs; use a separate data directory")
            if existing is None and self.external_review and (stored.get("thread_id") or stored.get("queue")):
                raise APIError(409, "workspace already contains Codex App Server activity")
            changed = existing is None
            if changed:
                stored["delivery_mode"] = mode
            if self.service_feedback_transport == "mcp_events" and "feedback_transport" not in stored:
                stored["feedback_transport"] = self.service_feedback_transport
                changed = True
            if changed:
                self.store._save()
            return copy.deepcopy(stored)

    def state(self, *, include_capability: bool = False, preferred_session_id: str | None = None) -> dict[str, Any]:
        self.ensure(preferred_session_id)
        result = self.store.workspace()
        result["feedback_transport"] = self.feedback_transport
        result["service_feedback_transport"] = self.service_feedback_transport
        if self.mcp_events is not None:
            result["event_delivery"] = self.mcp_events.status(result["session_id"])
            self._sync_event_queue(result)
        result.pop("events", None)
        result["events_cursor"] = result.pop("event_seq")
        result["object_prompts_supported"] = True
        result["inline_references_supported"] = True
        result["scene_references_supported"] = not getattr(self, "blank_workbench", False)
        result["image_references_supported"] = True
        result["dynamic_scenes_supported"] = True
        result["human_pose_supported"] = self.pose_jobs is not None
        result["human_pose_inference_supported"] = False
        result["human_pose_mode"] = "external_results"
        result["pose_corrections_supported"] = True
        connection_supported = self.external_review and not getattr(self, "blank_workbench", False)
        result["task_connection_supported"] = connection_supported
        result["task_creation_supported"] = connection_supported
        result["model_selection_supported"] = connection_supported
        result["desktop_available"] = self.external_review and self._desktop_available(result)
        result["project_creation_supported"] = self.service_feedback_transport != "mcp_events" and (
            result["desktop_available"] or not self.external_review and callable(getattr(self.adapter, "list_models", None)))
        if self.project_name:
            result["project_name"] = self.project_name
        result["reference_clip"] = self.store.get_session(result["session_id"]).get("reference_clip")
        for approval in result.get("approvals", []):
            approval.pop("request_id", None)
        if include_capability:
            result["browser_capability"] = self.store.browser_token
        return result

    def _sync_event_queue(self, result: dict[str, Any]) -> None:
        """Project event receipts are separate from Codex turn completion."""
        for item in result["queue"]:
            if item.get("feedback_transport") == "mcp_events":
                item.update(self.mcp_events.feedback_status(item["feedback_id"]))

    def start(self) -> None:
        self.ensure()
        if self.service_feedback_transport == "mcp_events" and self.mcp_events is None:
            raise RuntimeError("MCP events service is not configured")
        if self.mcp_events is not None:
            with self.store.lock:
                pending = [self.store.feedback_by_id(item["feedback_id"]) for item in self.store.state["workspace"]["queue"]
                           if item.get("feedback_transport") == "mcp_events"]
            if self.store.get_session(self.ensure()["session_id"])["status"] == "open":
                for packet in pending:
                    self.mcp_events.emit_feedback(packet)
            self.mcp_events.start()
        if self.feedback_transport == "mcp_events":
            self._started = True
            self.store.workspace_agent(status="external_idle")
            return
        self._restore_blocked_visual_feedback()
        self._started = True
        if self.external_review and self.adapter is None:
            self.store.workspace_agent(status="external_idle")
            return
        if self.adapter is None:
            self.store.workspace_agent(status="disconnected", error="Codex App Server is not configured")
            return
        if self.external_review:
            # A request that was waiting for an MCP call is still a durable
            # browser submission when this service becomes directly bound.
            with self.store.lock:
                workspace = self.store.state["workspace"]
                bound_thread_id = workspace.get("thread_id") or getattr(self.adapter, "thread_id", None)
                for item in workspace["queue"]:
                    if item.get("feedback_transport") == "mcp_events":
                        continue
                    # Old workspaces predate per-packet routing. Pin them to
                    # the task that owned the workspace before any switch.
                    if item.get("target_thread_id") is None:
                        item["target_thread_id"] = bound_thread_id
                    if item["status"] == "awaiting_mcp":
                        item["status"] = "queued"
                self.store._save()
            self._supervise_once()
            self._supervisor_thread = threading.Thread(target=self._supervisor_loop, daemon=True, name="workspace-supervisor")
            self._supervisor_thread.start()
            return
        try:
            thread_id = self.adapter.start()
            self.store.workspace_thread(thread_id)
            bound_task_idle = True
            if self.external_review:
                bound_task_idle = self.adapter.refresh().get("turn_state") == "idle"
            with self.store.lock:
                saved_workspace = self.store.state["workspace"]
                saved_active = saved_workspace.get("active_feedback_id")
                saved_item = next((entry for entry in saved_workspace["queue"] if entry["feedback_id"] == saved_active), None)
                saved_turn_id = saved_item.get("turn_id") if saved_item else None
            recovered_turn = None
            if self.external_review and saved_turn_id and hasattr(self.adapter, "lookup_turn"):
                try:
                    recovered_turn = self.adapter.lookup_turn(saved_turn_id)
                except Exception:
                    logging.exception("Could not reconcile the previous shared Codex turn")
            with self.store.lock:
                workspace = self.store.state["workspace"]
                active_id = workspace.get("active_feedback_id")
                workspace["approvals"] = []
                if self.external_review and bound_task_idle:
                    for queued in workspace["queue"]:
                        if queued["status"] == "awaiting_mcp":
                            queued["status"] = "queued"
                if active_id:
                    item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == active_id), None)
                    if item and item["status"] in {"dispatching", "running"}:
                        if recovered_turn and recovered_turn.get("id") == item.get("turn_id") and recovered_turn.get("status") in {"completed", "failed", "interrupted"}:
                            item["status"] = recovered_turn["status"]
                            item["error"] = None
                            workspace["active_feedback_id"] = None
                            workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                            self.store.workspace_event("turn_reconciled", {"feedback_id": active_id, "turn_id": item["turn_id"], "status": item["status"]})
                        else:
                            item["status"] = "delivery_uncertain"
                            item["error"] = "Gateway restarted during a turn; inspect the thread before retrying."
                            workspace["agent"] = {"status": "delivery_uncertain", "turn_id": item.get("turn_id"), "error": item["error"]}
                            self.store.workspace_event("delivery_uncertain", {"feedback_id": active_id, "message": item["error"]})
                        self.store._save()
                else:
                    workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                    self.store._save()
            self.wake()
        except Exception as exc:
            logging.exception("Could not connect local Codex App Server")
            self.store.workspace_agent(status="disconnected", error=str(exc)[:500])
            self.store.workspace_event("disconnected", {"message": str(exc)[:500]})

    def _restore_blocked_visual_feedback(self) -> None:
        """Resume saved visual packets held by the former revision gate.

        Only never-dispatched packets with immutable scene pixels are eligible.
        Active packets and packets with a turn ID remain in their existing
        reconciliation path, so restarting cannot replay a delivered message.
        """
        with self.store.lock:
            workspace = self.store.state["workspace"]
            feedback = {item["feedback_id"]: item for item in self.store.state["feedback"]}
            restored = []
            for item in workspace["queue"]:
                if item.get("feedback_transport") == "mcp_events":
                    continue
                if (item["status"] != "blocked_stale" or item.get("turn_id")
                        or item["feedback_id"] == workspace.get("active_feedback_id")):
                    continue
                packet = feedback.get(item["feedback_id"], {})
                has_evidence = bool(packet.get("scene_original_url")) or any(
                    frame.get("scene_original_url") for frame in [*packet.get("dynamic_frames", []), *packet.get("scene_snapshots", [])]
                )
                if not has_evidence:
                    continue
                item["status"] = "queued"
                item["error"] = None
                restored.append(item["feedback_id"])
            if restored:
                self.store._save()
                self.store.workspace_event("saved_visual_feedback_resumed", {"feedback_ids": restored})

    def close(self) -> None:
        if self.mcp_events is not None:
            self.mcp_events.close()
        if self.pose_jobs is not None:
            self.pose_jobs.close()
        with self._worker_lock:
            self._started = False
            worker = self._worker_thread
        self._supervisor_stop.set()
        if self._supervisor_thread is not None and self._supervisor_thread is not threading.current_thread():
            self._supervisor_thread.join(timeout=30)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=30)
        if self.adapter is not None:
            self.adapter.close()

    def _target_thread(self) -> str:
        if not self.external_review or self.adapter is None or not getattr(self.adapter, "thread_id", None):
            raise APIError(409, "this workspace is not bound to a Codex Desktop task")
        with self.store.lock:
            return self.store.state["workspace"].get("thread_id") or self.adapter.thread_id

    def _desktop_available(self, workspace: dict[str, Any]) -> bool:
        if workspace.get("thread_id") or getattr(self.adapter, "thread_id", None) or self.desktop_seed_thread_id:
            return True
        try:
            from shared_thread_bridge import _private_socket_candidates
            return len(_private_socket_candidates()) == 1
        except (OSError, SharedThreadBridgeError):
            return False

    def _desktop_entry(self) -> tuple[str | None, str]:
        """Find the Desktop daemon without binding its seed task to a new scene."""
        self.ensure()
        if not self.external_review:
            raise APIError(409, "this workspace is not bound to a Codex Desktop task")
        with self.store.lock:
            current = self.store.state["workspace"].get("thread_id") or getattr(self.adapter, "thread_id", None)
        seed = current or self.desktop_seed_thread_id
        # A neutral identity only selects/initializes the sole trusted daemon.
        # No existing task is resumed, adopted or bound while reading choices.
        return current, seed or "00000000-0000-0000-0000-000000000000"

    def _validate_target(self, thread_id: str) -> None:
        if self.target_validator is not None:
            self.target_validator(thread_id)

    def _target_available(self, thread_id: str) -> bool:
        try:
            self._validate_target(thread_id)
            return True
        except APIError:
            return False

    def _creation_config(self) -> dict[str, Any]:
        return {"config": copy.deepcopy(self.thread_config)} if self.thread_config is not None else {}

    def _owned_adapter_config(self, thread_id: str | None = None) -> dict[str, Any]:
        config = {"thread_config": copy.deepcopy(self.thread_config)} if self.thread_config is not None else {}
        with self.store.lock:
            workspace = self.store.state.get("workspace") or {}
            if thread_id in workspace.get("created_thread_ids", []):
                spec = workspace.get("created_thread_specs", {}).get(thread_id)
                mode = spec.get("permission_mode") if isinstance(spec, dict) else None
                if isinstance(mode, str) and mode in {"full_access", "workspace_write", "read_only"}:
                    config["permission_mode"] = mode
        return config

    def scoped_adapter_callback(self, token: object | None = None):
        """Ignore events from a Desktop adapter after it has been replaced.

        The token is per adapter instance, not per task ID: switching away
        and later back to the same task must not revive an old listener.
        """
        expected = self._adapter_token if token is None else token

        def receive(event: dict[str, Any]) -> None:
            with self._adapter_event_lock:
                if expected is self._adapter_token:
                    self.on_adapter_event(event)

        return receive

    def _target_cwd_allowed(self, thread: dict[str, Any]) -> bool:
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return False
        try:
            raw_directory = Path(cwd).expanduser()
            if not raw_directory.is_absolute():
                return False
            directory = raw_directory.resolve()
        except (OSError, RuntimeError):
            return False
        return directory == self.project_dir or directory in self.project_dir.parents or self.project_dir in directory.parents

    @staticmethod
    def _target_accepts_direct_input(thread: dict[str, Any]) -> bool:
        """Exclude child agents, which are not user-owned Desktop tasks."""
        return (thread.get("threadSource") != "subagent"
                and thread.get("canAcceptDirectInput") is not False
                and not thread.get("parentThreadId"))

    @staticmethod
    def _target_summary(thread: dict[str, Any]) -> dict[str, Any]:
        thread_id = thread["id"]
        name = thread.get("name")
        title = name.strip()[:160] if isinstance(name, str) else ""
        status = thread.get("status")
        return {
            "thread_id": thread_id,
            "title": title or f"未命名任务 · {thread_id[:13]}",
            "model": thread.get("model") if isinstance(thread.get("model"), str) else None,
            "reasoning_effort": thread.get("reasoningEffort") if isinstance(thread.get("reasoningEffort"), str) else None,
            "status": status.get("type") if isinstance(status, dict) else None,
        }

    def list_targets(self) -> dict[str, Any]:
        """Show compatible user tasks and the current task's saved title."""
        current_id, seed_id = self._desktop_entry()
        try:
            discovered = SharedThreadBridge.discover_loaded_threads()
        except Exception as exc:
            raise APIError(503, f"cannot list loaded Codex Desktop tasks: {exc}") from exc
        current_paths = {path for path, thread in discovered if thread.get("id") == seed_id}
        loaded_paths = {path for path, _thread in discovered}
        if len(current_paths) > 1 or (not current_paths and len(loaded_paths) > 1):
            raise APIError(409, "cannot identify a single Codex Desktop daemon for this workspace")
        counts: dict[str, int] = {}
        for _path, thread in discovered:
            task_id = thread.get("id")
            if isinstance(task_id, str):
                counts[task_id] = counts.get(task_id, 0) + 1
        candidates = []
        for path, thread in discovered:
            thread_id = thread.get("id")
            if (not isinstance(thread_id, str) or counts.get(thread_id) != 1
                    or (current_paths and path not in current_paths)
                    or not self._target_cwd_allowed(thread)
                    or not self._target_available(thread_id)
                    or not self._target_accepts_direct_input(thread)):
                continue
            candidates.append(self._target_summary(thread))
        with self.store.lock:
            owned_ids = tuple(self.store.state["workspace"].get("created_thread_ids", []))
        loaded_ids = {item["thread_id"] for item in candidates}
        missing_ids = [thread_id for thread_id in dict.fromkeys((current_id, *owned_ids)) if thread_id and thread_id not in loaded_ids and self._target_available(thread_id)]
        if missing_ids:
            try:
                bridge = SharedThreadBridge.connect_to_desktop(seed_id)
                try:
                    for thread_id in missing_ids:
                        try:
                            thread = bridge.read_loaded_thread(thread_id)
                        except SharedThreadRPCRejected as exc:
                            if thread_id in owned_ids and "no rollout found" in str(exc).lower():
                                with self.store.lock:
                                    spec = self.store.state["workspace"].get("created_thread_specs", {}).get(thread_id)
                                if isinstance(spec, dict):
                                    name = spec.get("title")
                                    title = name.strip()[:160] if isinstance(name, str) else ""
                                    candidates.append({"thread_id": thread_id, "title": title or f"未命名任务 · {thread_id[:13]}", "model": spec.get("model"), "reasoning_effort": spec.get("reasoning_effort"), "status": "recoverable"})
                            continue
                        except Exception:
                            continue
                        if not self._target_cwd_allowed(thread) or not self._target_accepts_direct_input(thread):
                            continue
                        candidates.append(self._target_summary(thread))
                finally:
                    bridge.close()
            except Exception:
                logging.exception("Could not inspect unloaded Codex tasks")
        return {"targets": candidates, "thread_id": current_id}

    @staticmethod
    def _visible_models(bridge: SharedThreadBridge) -> dict[str, Any]:
        models = []
        for item in bridge.list_models():
            model = item.get("model") or item.get("id")
            modalities = item.get("inputModalities", ["text", "image"])
            if (not isinstance(model, str) or not model or item.get("hidden") is True
                    or not isinstance(modalities, list) or "image" not in modalities):
                continue
            raw_efforts = item.get("supportedReasoningEfforts")
            efforts = [entry["reasoningEffort"] for entry in (raw_efforts if isinstance(raw_efforts, list) else [])
                       if isinstance(entry, dict) and isinstance(entry.get("reasoningEffort"), str)]
            default_effort = item.get("defaultReasoningEffort")
            if not isinstance(default_effort, str):
                default_effort = efforts[0] if efforts else None
            models.append({
                "model": model,
                "display_name": item.get("displayName") if isinstance(item.get("displayName"), str) else model,
                "default_reasoning_effort": default_effort,
                "supported_reasoning_efforts": efforts,
                "is_default": item.get("isDefault") is True,
            })
        default_model = next((entry["model"] for entry in models if entry["is_default"]), None)
        return {"models": models, "default_model": default_model or (models[0]["model"] if models else None)}

    def list_models(self) -> dict[str, Any]:
        if not self.external_review:
            if not callable(getattr(self.adapter, "list_models", None)):
                raise APIError(409, "Codex CLI is not configured for this workspace")
            try:
                return {**self._visible_models(self.adapter), "permission_modes_supported": True}
            except Exception as exc:
                raise APIError(503, f"cannot list Codex CLI models: {exc}") from exc
        _, current_id = self._desktop_entry()
        try:
            bridge = SharedThreadBridge.connect_to_desktop(current_id)
            try:
                return {**self._visible_models(bridge), "permission_modes_supported": True}
            finally:
                bridge.close()
        except Exception as exc:
            raise APIError(503, f"cannot list Codex Desktop models: {exc}") from exc

    @staticmethod
    def _check_switchable_workspace(workspace: dict[str, Any], current_id: str | None) -> None:
        if workspace.get("thread_id") != current_id:
            raise APIError(409, "workspace target changed; reload before switching")
        if workspace.get("active_feedback_id") or any(
            item["status"] in {"dispatching", "running"}
            or (item["status"] == "delivery_uncertain" and not item.get("quarantined_at"))
            for item in workspace["queue"]
        ):
            raise APIError(409, "finish or resolve the active feedback before switching tasks")

    def _commit_target(self, current_id: str | None, thread_id: str, replacement: SharedDesktopAdapter, replacement_token: object) -> Any:
        """Commit a prevalidated binding while supervisor, event, and worker locks are held."""
        with self.target_binding_lock, self.store.lock:
            self._validate_target(thread_id)
            workspace = self.store.state["workspace"]
            self._check_switchable_workspace(workspace, current_id)
            previous_workspace = copy.deepcopy(workspace)
            old_adapter = self.adapter
            old_initialized = self._adapter_initialized
            old_token = self._adapter_token
            try:
                for item in workspace["queue"]:
                    if item.get("feedback_transport") == "mcp_events":
                        continue
                    if item.get("target_thread_id") is None:
                        item["target_thread_id"] = current_id
                workspace["thread_id"] = thread_id
                workspace["task_connection_mode"] = "desktop"
                if self.service_feedback_transport == "mcp_events":
                    workspace["feedback_transport_origin"] = "mcp_events"
                    workspace["feedback_transport"] = "legacy"
                workspace["approvals"] = []
                workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                workspace["event_seq"] += 1
                workspace["events"].append({"id": workspace["event_seq"], "type": "target_switched", "payload": {"old_thread_id": current_id, "thread_id": thread_id}, "at": _now()})
                workspace["events"] = workspace["events"][-500:]
                self.store._save()
                self.adapter = replacement
                self.feedback_transport = "legacy"
                self._adapter_token = replacement_token
                self._adapter_initialized = True
                return old_adapter
            except Exception:
                self.store.state["workspace"] = previous_workspace
                self.adapter = old_adapter
                self._adapter_initialized = old_initialized
                self._adapter_token = old_token
                raise

    def _recover_empty_owned_task(self, missing_id: str, *, current_id: str | None, desktop_seed_id: str | None = None) -> None:
        """Replace only a workbench-owned task proven to have no rollout.

        Called with the supervisor lock held.  No packet that might have
        reached Codex may be rerouted by this path.
        """
        with self._adapter_event_lock, self._worker_lock, self.target_binding_lock:
            with self.store.lock:
                workspace = self.store.state["workspace"]
                spec = workspace.get("created_thread_specs", {}).get(missing_id)
                pending_replacement = next((thread_id for thread_id, options in workspace.get("created_thread_specs", {}).items()
                                            if isinstance(options, dict) and options.get("replaces") == missing_id), None)
                if (workspace.get("thread_id") != current_id or not isinstance(spec, dict)
                        or self._worker_running or workspace.get("active_feedback_id")
                        or any(item["status"] in {"dispatching", "running"} or
                               (item["status"] == "delivery_uncertain" and not item.get("quarantined_at"))
                               for item in workspace["queue"])
                        or any(item.get("target_thread_id") == missing_id and
                               (item.get("status") not in {"queued", "blocked_stale", "awaiting_mcp"} or item.get("turn_id"))
                               for item in workspace["queue"])):
                    raise SharedThreadBridgeError("empty task cannot be recreated after a feedback delivery attempt")
                if pending_replacement:
                    raise SharedThreadBridgeError(f"replacement task {pending_replacement} already exists; select it from the task picker")
                spec = copy.deepcopy(spec)
            bridge = SharedThreadBridge.connect_to_desktop(current_id or desktop_seed_id or self.desktop_seed_thread_id)
            replacement: SharedDesktopAdapter | None = None
            try:
                catalog = self._visible_models(bridge)
                selection = next((item for item in catalog["models"] if item["model"] == spec.get("model")), None)
                if selection is None or spec.get("reasoning_effort") not in selection["supported_reasoning_efforts"]:
                    raise SharedThreadBridgeError("saved model or reasoning effort is no longer available")
                # Legacy specs did not record permissions.  Preserve the
                # narrower behavior when recreating them instead of silently
                # granting workspace writes.
                spec.setdefault("permission_mode", "read_only")
                permission_error = None
                try:
                    new_id = bridge.create_thread(
                        spec["model"],
                        self.project_dir,
                        reasoning_effort=spec["reasoning_effort"],
                        title=spec.get("title"),
                        permission_mode=spec["permission_mode"],
                        **self._creation_config(),
                    )
                except SharedThreadPermissionMismatch as exc:
                    if exc.created_thread_id is None:
                        raise
                    new_id = exc.created_thread_id
                    permission_error = exc
                self._validate_target(new_id)
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    owned = workspace.setdefault("created_thread_ids", [])
                    specs = workspace.setdefault("created_thread_specs", {})
                    owned.append(new_id)
                    specs[new_id] = {**spec, "replaces": missing_id}
                    try:
                        self.store._save()
                    except Exception:
                        owned.remove(new_id)
                        specs.pop(new_id, None)
                        raise
                if permission_error is not None:
                    raise permission_error
                if bridge.read_thread().get("status", {}).get("type") != "idle":
                    raise SharedThreadBridgeError("replacement empty task is not idle")
                token = object()
                replacement = SharedDesktopAdapter(new_id, on_event=self.scoped_adapter_callback(token), allow_owned_resume=True, initial_bridge=bridge, **self._owned_adapter_config(new_id))
                replacement.start()
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    if (workspace.get("thread_id") != current_id or workspace.get("active_feedback_id")
                            or any(item["status"] in {"dispatching", "running"} or
                                   (item["status"] == "delivery_uncertain" and not item.get("quarantined_at"))
                                   for item in workspace["queue"])
                            or any(item.get("target_thread_id") == missing_id and
                                   (item.get("status") not in {"queued", "blocked_stale", "awaiting_mcp"} or item.get("turn_id"))
                                   for item in workspace["queue"])):
                        raise SharedThreadBridgeError("feedback state changed during empty task recovery")
                    previous = copy.deepcopy(workspace)
                    old_adapter = self.adapter
                    old_initialized = self._adapter_initialized
                    old_token = self._adapter_token
                    try:
                        owned = workspace.setdefault("created_thread_ids", [])
                        owned[:] = [thread_id for thread_id in owned if thread_id != missing_id]
                        specs = workspace.setdefault("created_thread_specs", {})
                        specs.pop(missing_id, None)
                        specs[new_id] = spec
                        for item in workspace["queue"]:
                            if item.get("feedback_transport") == "mcp_events":
                                continue
                            if item.get("target_thread_id") is None:
                                item["target_thread_id"] = current_id
                            if item.get("target_thread_id") == missing_id:
                                item["target_thread_id"] = new_id
                        workspace["thread_id"] = new_id
                        workspace["task_connection_mode"] = "desktop"
                        if self.service_feedback_transport == "mcp_events":
                            workspace["feedback_transport_origin"] = "mcp_events"
                            workspace["feedback_transport"] = "legacy"
                        workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                        workspace["event_seq"] += 1
                        workspace["events"].append({"id": workspace["event_seq"], "type": "empty_thread_recreated", "payload": {"old_thread_id": missing_id, "previous_bound_thread_id": current_id, "thread_id": new_id}, "at": _now()})
                        workspace["events"] = workspace["events"][-500:]
                        self.store._save()
                        self.adapter = replacement
                        self.feedback_transport = "legacy"
                        self._adapter_token = token
                        self._adapter_initialized = True
                    except Exception:
                        self.store.state["workspace"] = previous
                        self.adapter = old_adapter
                        self._adapter_initialized = old_initialized
                        self._adapter_token = old_token
                        raise
            finally:
                if replacement is None or self.adapter is not replacement:
                    if replacement is not None:
                        replacement.close()
                    else:
                        bridge.close()
            try:
                if old_adapter is not None:
                    old_adapter.close()
            except Exception:
                logging.exception("Could not close missing empty Codex task adapter")
        if self._started and self._supervisor_thread is None:
            self.start()
        self.wake()

    def create_target(
        self,
        model: Any,
        *,
        reasoning_effort: Any = None,
        title: Any = None,
        permission_mode: Any = "workspace_write",
    ) -> dict[str, Any]:
        """Create a fresh Desktop task only after an explicit user action."""
        with self._route_lock:
            return self._create_target_bound(model, reasoning_effort=reasoning_effort, title=title, permission_mode=permission_mode)

    def _create_target_bound(self, model: Any, *, reasoning_effort: Any = None, title: Any = None,
                             permission_mode: Any = "workspace_write") -> dict[str, Any]:
        if getattr(self, "blank_workbench", False):
            raise APIError(409, "select an imported scene before creating a Codex task")
        current_id, seed_id = self._desktop_entry()
        if not isinstance(model, str) or not model:
            raise APIError(400, "select a Codex model")
        if not isinstance(permission_mode, str) or permission_mode not in {"full_access", "workspace_write", "read_only"}:
            raise APIError(400, "permission_mode must be full_access, workspace_write, or read_only")
        if reasoning_effort is not None and not isinstance(reasoning_effort, str):
            raise APIError(400, "reasoning_effort must be a supported value")
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title.strip()) > 120 or any(ord(ch) < 32 for ch in title)):
            raise APIError(400, "title must contain 1 to 120 printable characters")
        name = title.strip() if isinstance(title, str) else f"Scene feedback · {self.project_dir.name}"[:120]
        created_id: str | None = None
        replacement: SharedDesktopAdapter | None = None
        old_adapter: Any = None
        with self._supervisor_lock, self._adapter_event_lock, self._worker_lock:
            with self.store.lock:
                self._check_switchable_workspace(self.store.state["workspace"], current_id)
                if self._worker_running:
                    raise APIError(409, "finish active feedback delivery before creating a task")
            try:
                bridge = SharedThreadBridge.connect_to_desktop(seed_id)
            except Exception as exc:
                raise APIError(503, f"cannot connect to Codex Desktop: {exc}") from exc
            try:
                catalog = self._visible_models(bridge)
                selection = next((item for item in catalog["models"] if item["model"] == model), None)
                if selection is None:
                    raise APIError(400, "selected model is not available with image input")
                efforts = selection["supported_reasoning_efforts"]
                effort = reasoning_effort if reasoning_effort is not None else selection["default_reasoning_effort"]
                if effort is not None and effort not in efforts:
                    raise APIError(400, "reasoning_effort is not supported by the selected model")
                permission_error = None
                try:
                    created_id = bridge.create_thread(
                        model,
                        self.project_dir,
                        reasoning_effort=effort,
                        title=name,
                        permission_mode=permission_mode,
                        **self._creation_config(),
                    )
                except SharedThreadPermissionMismatch as exc:
                    if exc.created_thread_id is None:
                        raise
                    created_id = exc.created_thread_id
                    permission_error = exc
                except SharedThreadRPCRejected as exc:
                    raise APIError(409, f"Codex Desktop rejected task creation: {exc}") from exc
                except SharedThreadBridgeError as exc:
                    raise APIError(503, f"task creation result is uncertain; inspect Codex Desktop before trying again: {exc}") from exc
                # Persist ownership immediately.  If later binding fails the
                # task remains recoverable in the picker, including after a
                # service restart or Desktop's inactivity unload.
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    owned = workspace.setdefault("created_thread_ids", [])
                    if created_id not in owned:
                        owned.append(created_id)
                        workspace.setdefault("created_thread_specs", {})[created_id] = {
                            "model": model,
                            "reasoning_effort": effort,
                            "title": name,
                            "permission_mode": permission_mode,
                        }
                        try:
                            self.store._save()
                        except Exception:
                            owned.remove(created_id)
                            workspace["created_thread_specs"].pop(created_id, None)
                            raise
                if permission_error is not None:
                    raise APIError(503, str(permission_error)) from permission_error
                replacement_token = object()
                replacement = SharedDesktopAdapter(created_id, on_event=self.scoped_adapter_callback(replacement_token), allow_owned_resume=True, initial_bridge=bridge, **self._owned_adapter_config(created_id))
                replacement.start()
                if replacement.inspect_thread_status() != "idle":
                    raise APIError(409, "new Codex task is not idle")
                old_adapter = self._commit_target(current_id, created_id, replacement, replacement_token)
            except APIError as exc:
                if created_id is not None:
                    raise APIError(exc.status, f"{exc.message}; created task can be selected in the picker", detail={"thread_id": created_id}) from exc
                raise
            except Exception as exc:
                if created_id is not None:
                    raise APIError(503, f"created Codex task, but could not bind it: {exc}; choose it from the task picker", detail={"thread_id": created_id}) from exc
                raise APIError(503, f"cannot create Codex task: {exc}") from exc
            finally:
                if replacement is not None and self.adapter is not replacement:
                    replacement.close()
                if replacement is None:
                    bridge.close()
        if old_adapter is not None:
            try:
                old_adapter.close()
            except Exception:
                logging.exception("Could not close previous Codex task adapter after creating a task")
        if self._started and self._supervisor_thread is None:
            self.start()
        self.wake()
        return {"thread_id": created_id, "workspace": self.state()}

    def switch_target(self, thread_id: Any) -> dict[str, Any]:
        """Route future feedback to another idle task without moving old packets."""
        with self._route_lock:
            return self._switch_target_bound(thread_id)

    def _switch_target_bound(self, thread_id: Any) -> dict[str, Any]:
        if getattr(self, "blank_workbench", False):
            raise APIError(409, "select an imported scene before connecting a Codex task")
        current_id, seed_id = self._desktop_entry()
        if not isinstance(thread_id, str):
            raise APIError(400, "thread_id must be a Codex task UUID")
        try:
            if str(uuid.UUID(thread_id)) != thread_id:
                raise ValueError("noncanonical task UUID")
        except ValueError as exc:
            raise APIError(400, "thread_id must be a Codex task UUID") from exc
        if thread_id == current_id:
            return self.state()
        self._validate_target(thread_id)
        try:
            discovered = SharedThreadBridge.discover_loaded_threads()
            current_paths = {path for path, thread in discovered if thread.get("id") == seed_id}
            loaded_paths = {path for path, _thread in discovered}
            if len(current_paths) > 1 or (not current_paths and len(loaded_paths) > 1):
                raise APIError(409, "cannot identify a single Codex Desktop daemon for this workspace")
            matches = [(path, thread) for path, thread in discovered if thread.get("id") == thread_id]
            with self.store.lock:
                owned = thread_id in self.store.state["workspace"].get("created_thread_ids", [])
            if not matches and owned:
                bridge = SharedThreadBridge.connect_to_desktop(seed_id)
                try:
                    try:
                        matches = [(bridge.socket_path, bridge.read_loaded_thread(thread_id))]
                    except SharedThreadRPCRejected as exc:
                        if "no rollout found" not in str(exc).lower():
                            raise
                        # The user explicitly selected a workbench-owned
                        # empty task that vanished after Desktop unloaded it.
                        # A fresh zero-turn task can safely replace it.
                        with self._supervisor_lock:
                            self._recover_empty_owned_task(thread_id, current_id=current_id, desktop_seed_id=seed_id)
                        return self.state()
                finally:
                    bridge.close()
            if len(matches) != 1 or (current_paths and matches[0][0] not in current_paths):
                raise APIError(409, "target task is not available in the same Codex Desktop daemon")
            target = matches[0][1]
            if not self._target_accepts_direct_input(target):
                raise APIError(409, "target is a Codex subagent and cannot receive direct input")
            if not self._target_cwd_allowed(target):
                raise APIError(409, "target task cwd cannot access this workspace project")
            status = target.get("status")
            if not isinstance(status, dict) or status.get("type") not in ({"idle", "notLoaded"} if owned else {"idle"}):
                raise APIError(409, "target Codex task is active; switch after it becomes idle")
        except APIError:
            raise
        except ValueError as exc:
            raise APIError(400, "thread_id must be a Codex task UUID") from exc
        except Exception as exc:
            raise APIError(503, f"cannot inspect Codex Desktop target: {exc}") from exc

        with self.store.lock:
            owned = thread_id in self.store.state["workspace"].get("created_thread_ids", [])
        replacement_token = object()
        kwargs = {"allow_owned_resume": True} if owned else {"allow_bound_resume": True}
        kwargs.update(self._owned_adapter_config(thread_id))
        replacement = SharedDesktopAdapter(thread_id, on_event=self.scoped_adapter_callback(replacement_token), **kwargs)
        try:
            try:
                replacement.start()
            except Exception as exc:
                raise APIError(503, f"target Codex task is no longer available: {exc}") from exc
            with self._supervisor_lock, self._adapter_event_lock:
                with self._worker_lock:
                    with self.store.lock:
                        self._check_switchable_workspace(self.store.state["workspace"], current_id)
                        if self._worker_running:
                            raise APIError(409, "finish active feedback delivery before switching tasks")
                    try:
                        runtime_status = replacement.inspect_thread_status()
                    except Exception as exc:
                        raise APIError(503, f"cannot verify target Codex task status: {exc}") from exc
                    if runtime_status != "idle":
                        raise APIError(409, "target Codex task became active; switch after it becomes idle")
                    old_adapter = self._commit_target(current_id, thread_id, replacement, replacement_token)
        except Exception:
            if self.adapter is not replacement:
                replacement.close()
            raise
        try:
            if old_adapter is not None:
                old_adapter.close()
        except Exception:
            logging.exception("Could not close previous Codex task adapter after switching")
        if self._started and self._supervisor_thread is None:
            self.start()
        self.wake()
        return self.state()

    def _supervisor_loop(self) -> None:
        while not self._supervisor_stop.wait(self._RECONCILE_INTERVAL_SEC):
            try:
                self._supervise_once()
            except Exception:
                logging.exception("Workspace supervisor failed; retrying")

    def _supervise_once(self) -> None:
        if not self._started or self.adapter is None or not self.external_review:
            return
        if not self._supervisor_lock.acquire(blocking=False):
            return
        try:
            try:
                if not self._adapter_initialized or not self.adapter.status().get("connected"):
                    thread_id = self.adapter.start()
                    self.store.workspace_thread(thread_id)
                    with self.store.lock:
                        workspace = self.store.state["workspace"]
                        unpinned = [item for item in workspace["queue"] if item.get("target_thread_id") is None
                                    and item.get("feedback_transport") != "mcp_events"]
                        if unpinned:
                            for item in unpinned:
                                item["target_thread_id"] = thread_id
                            self.store._save()
                    self._adapter_initialized = True
            except Exception as exc:
                if isinstance(exc, OwnedEmptyThreadMissing):
                    try:
                        self._recover_empty_owned_task(self.adapter.thread_id, current_id=self.adapter.thread_id)
                        return
                    except Exception as recovery_exc:
                        exc = recovery_exc
                error = str(exc)[:500]
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    active_id = workspace.get("active_feedback_id")
                    if not active_id and (workspace["agent"].get("status") != "disconnected"
                                          or workspace["agent"].get("error") != error):
                        workspace["agent"] = {"status": "disconnected", "turn_id": None, "error": error}
                        self.store._save()
                        self.store.workspace_event("disconnected", {"message": error})
                if active_id:
                    self._mark_uncertain(active_id, "Cannot inspect the Codex task while disconnected; checking again.")
                return
            with self.store.lock:
                workspace = self.store.state["workspace"]
                active_id = workspace.get("active_feedback_id")
                active = next((item for item in workspace["queue"] if item["feedback_id"] == active_id), None)
                active = copy.deepcopy(active) if active else None
                target_id = workspace.get("thread_id")
                quarantined = [item["feedback_id"] for item in workspace["queue"] if item["status"] == "delivery_uncertain" and item.get("quarantined_at") and item["feedback_id"] != active_id and item.get("target_thread_id") == target_id]
            if active:
                self._reconcile_active(active)
            elif active_id:
                with self.store.lock:
                    if self.store.state["workspace"].get("active_feedback_id") == active_id:
                        self.store.state["workspace"]["active_feedback_id"] = None
                        self.store._save()
            for feedback_id in quarantined:
                self._reconcile_quarantined(feedback_id)
            with self.store.lock:
                workspace = self.store.state["workspace"]
                if not workspace.get("active_feedback_id"):
                    pending = any(item["status"] == "queued" and item.get("feedback_transport") != "mcp_events"
                                  and item.get("target_thread_id") == workspace.get("thread_id") for item in workspace["queue"])
                    if workspace["agent"].get("status") == "disconnected" or (workspace["agent"].get("status") == "waiting" and not pending):
                        workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                        self.store._save()
                else:
                    pending = False
            if pending:
                self.wake()
        finally:
            self._supervisor_lock.release()

    def _matching_turn(self, feedback_id: str, turn_id: str | None) -> tuple[dict[str, Any] | None, bool]:
        """Return an exact saved turn, and whether multiple deliveries exist."""
        if hasattr(self.adapter, "lookup_feedback"):
            matches = self.adapter.lookup_feedback(feedback_id)
            if len(matches) > 1:
                return None, True
            if matches:
                return matches[0], False
        if turn_id and hasattr(self.adapter, "lookup_turn"):
            return self.adapter.lookup_turn(turn_id), False
        return None, False

    def _apply_reconciled_turn(self, feedback_id: str, turn: dict[str, Any]) -> None:
        turn_id = turn.get("id")
        outcome = turn.get("status")
        if not isinstance(turn_id, str) or outcome not in {"inProgress", "completed", "failed", "interrupted"}:
            return
        terminal = outcome != "inProgress"
        with self.store.lock:
            workspace = self.store.state["workspace"]
            item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == feedback_id), None)
            if item is None or item["status"] == "discarded":
                return
            if not terminal and workspace.get("active_feedback_id") not in {None, feedback_id}:
                return
            target_status = "running" if not terminal else outcome
            if item["status"] == target_status and item.get("turn_id") == turn_id:
                return
            item["status"] = target_status
            item["turn_id"] = turn_id
            item["error"] = None if outcome == "completed" else item.get("error")
            item.pop("quarantined_at", None)
            item.pop("uncertain_since", None)
            if terminal:
                if workspace.get("active_feedback_id") == feedback_id:
                    workspace["active_feedback_id"] = None
                    workspace["approvals"] = []
                    workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
            else:
                workspace["active_feedback_id"] = feedback_id
                workspace["agent"] = {"status": "running", "turn_id": turn_id, "error": None}
            self.store._save()
            self.store.workspace_event("turn_reconciled", {"feedback_id": feedback_id, "turn_id": turn_id, "status": item["status"]})
        if terminal and hasattr(self.adapter, "release_uncertain"):
            try:
                self.adapter.release_uncertain()
            except Exception:
                logging.exception("Could not reset reconciled adapter state")

    def _reconcile_active(self, item: dict[str, Any]) -> None:
        feedback_id = item["feedback_id"]
        if item["status"] == "dispatching":
            with self._worker_lock:
                if self._worker_running:
                    return
        try:
            turn, duplicate = self._matching_turn(feedback_id, item.get("turn_id"))
            if turn and not duplicate:
                self._apply_reconciled_turn(feedback_id, turn)
                return
            if not duplicate and item["status"] == "running" and self.adapter.status().get("turn_state") in {"active", "running"}:
                return
            runtime_status = self.adapter.inspect_thread_status() if hasattr(self.adapter, "inspect_thread_status") else self.adapter.refresh().get("turn_state")
        except Exception:
            logging.exception("Could not reconcile feedback %s", feedback_id)
            return
        self._mark_uncertain(feedback_id, "Multiple Codex turns carry this feedback ID; inspect the task before retrying." if duplicate else "Delivery could not be confirmed in the Codex task; checking again.")
        with self.store.lock:
            current = next((entry for entry in self.store.state["workspace"]["queue"] if entry["feedback_id"] == feedback_id), None)
            since = current.get("uncertain_since") if current else None
        if runtime_status != "idle" or not since:
            return
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age < self._UNCERTAIN_GRACE_SEC:
            return
        with self.store.lock:
            workspace = self.store.state["workspace"]
            current = next((entry for entry in workspace["queue"] if entry["feedback_id"] == feedback_id), None)
            if workspace.get("active_feedback_id") != feedback_id or current is None or current["status"] != "delivery_uncertain":
                return
            current["quarantined_at"] = _now()
            workspace["active_feedback_id"] = None
            workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
            self.store._save()
            self.store.workspace_event("uncertain_feedback_quarantined", {"feedback_id": feedback_id})
        if hasattr(self.adapter, "release_uncertain"):
            try:
                self.adapter.release_uncertain()
            except Exception:
                logging.exception("Could not release quarantined delivery")

    def _reconcile_quarantined(self, feedback_id: str) -> None:
        if not hasattr(self.adapter, "lookup_feedback"):
            return
        try:
            turn, duplicate = self._matching_turn(feedback_id, None)
        except Exception:
            logging.exception("Could not inspect quarantined feedback %s", feedback_id)
            return
        if duplicate:
            self._mark_uncertain(feedback_id, "Multiple Codex turns carry this feedback ID; inspect the task before retrying.")
        elif turn:
            self._apply_reconciled_turn(feedback_id, turn)

    def _mark_uncertain(self, feedback_id: str, message: str) -> None:
        with self.store.lock:
            workspace = self.store.state["workspace"]
            item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == feedback_id), None)
            if item is None or item["status"] in {"completed", "failed", "interrupted", "discarded"}:
                return
            changed = item["status"] != "delivery_uncertain"
            item["status"] = "delivery_uncertain"
            item["error"] = message
            item.setdefault("uncertain_since", _now())
            if workspace.get("active_feedback_id") == feedback_id:
                workspace["agent"] = {"status": "delivery_uncertain", "turn_id": item.get("turn_id"), "error": message}
            self.store._save()
            if changed:
                self.store.workspace_event("delivery_uncertain", {"feedback_id": feedback_id, "message": message})

    def submit(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._route_lock:
            return self._submit_bound(session_id, payload)

    def _submit_bound(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self.ensure(preferred_session_id=session_id)
        if session_id != workspace["session_id"]:
            raise APIError(409, "feedback belongs to another session; use the workspace session")
        if not payload.get("idempotency_key"):
            raise APIError(400, "idempotency_key is required for direct Codex delivery")
        if self.feedback_transport == "mcp_events" and self.mcp_events is None:
            raise APIError(503, "MCP events service is not configured")
        feedback = self.store.submit_feedback(session_id, payload)
        # Idempotent retries keep the original packet's delivery route even
        # after the scene was explicitly connected to a Desktop task.
        with self.store.lock:
            queued = next(entry for entry in self.store.state["workspace"]["queue"] if entry["feedback_id"] == feedback["feedback_id"])
            event_packet = queued.get("feedback_transport") == "mcp_events"
        if event_packet:
            if self.mcp_events is None:
                raise APIError(503, "MCP events service is not configured")
            delivery = self.mcp_events.emit_feedback(feedback)
            result = copy.deepcopy(feedback)
            result["delivery"] = delivery
            return result
        if self.external_review and self.adapter is not None:
            bound_id = workspace.get("thread_id") or getattr(self.adapter, "thread_id", None)
            if bound_id:
                with self.store.lock:
                    item = next(entry for entry in self.store.state["workspace"]["queue"] if entry["feedback_id"] == feedback["feedback_id"])
                    if item.get("target_thread_id") is None:
                        item["target_thread_id"] = bound_id
                        self.store._save()
        if self.external_review and self.adapter is None:
            with self.store.lock:
                item = next(entry for entry in self.store.state["workspace"]["queue"] if entry["feedback_id"] == feedback["feedback_id"])
                if item["status"] == "queued":
                    item["status"] = "awaiting_mcp"
                    self.store._save()
                result = copy.deepcopy(feedback)
                result["delivery"] = copy.deepcopy(item)
                return result
        self.wake()
        with self.store.lock:
            item = next(entry for entry in self.store.state["workspace"]["queue"] if entry["feedback_id"] == feedback["feedback_id"])
            result = copy.deepcopy(feedback)
            result["delivery"] = copy.deepcopy(item)
            return result

    def _project_file(self, local_path: Any) -> Path:
        if not isinstance(local_path, str) or not local_path:
            raise APIError(400, "local path must be a file path")
        try:
            path = Path(local_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise APIError(400, "local file does not exist") from exc
        if not path.is_file() or not path.is_relative_to(self.project_dir):
            raise APIError(400, "local file must be inside the workspace project directory")
        return path

    def add_reference_paths(self, paths: Any) -> dict[str, Any]:
        """Import local images for the bound workspace, avoiding duplicate uploads."""
        workspace = self.ensure()
        if not isinstance(paths, list) or len(paths) > MAX_REFERENCES:
            raise APIError(400, "reference_images must be an array of at most 8 paths")
        manifest = self._reference_camera_manifest()
        prepared = []
        for path in paths:
            source = self._project_file(path)
            _, data = self.store._read_reference_path(str(source))
            # Camera exports often share names such as 000480.jpg. Keep the
            # view name visible in the browser's reference strip.
            label = f"{source.parent.name}_{source.name}"[-180:]
            prepared.append((label, data, self._manifest_camera(label, data, manifest)))
        with self.store.lock:
            session_id = workspace["session_id"]
            current = self.store.state["sessions"][session_id]["reference_images"]
            existing = {(entry["name"], (self.store.media_dir / entry["url"].rsplit("/", 1)[-1]).read_bytes()): entry for entry in current}
            novel = [(name, data, camera) for name, data, camera in prepared if (name, data) not in existing]
            if len(current) + len({(name, data) for name, data, _ in novel}) > MAX_REFERENCES:
                raise APIError(400, "workspace may contain at most 8 reference images")
            for name, data, _ in novel:
                if (name, data) in existing:
                    continue
                mime = self.store._image_kind(data)[1]
                data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                existing[(name, data)] = self.store.add_reference(session_id, name, data_url)
            camera_updates = list({existing[(name, data)]["id"]: {"reference_id": existing[(name, data)]["id"], "camera": camera}
                                   for name, data, camera in prepared if camera is not None and existing[(name, data)].get("camera") != camera}.values())
            if camera_updates:
                self.store.set_reference_cameras(session_id, camera_updates)
            return {"session_id": session_id, "reference_images": copy.deepcopy(self.store.state["sessions"][session_id]["reference_images"])}

    def set_reference_clip_paths(self, payload: Any) -> dict[str, Any]:
        """Validate every project-scoped view before publishing the complete group."""
        from dynamic import MAX_CLIP_MANIFEST_BYTES, MAX_LOCAL_CLIP_BYTES, MAX_MULTIVIEW_MANIFEST_BYTES, MAX_REFERENCE_VIEWS, clip_name, fps_value, number, prepare_clip, sample_video

        workspace = self.ensure()
        if not isinstance(payload, dict):
            raise APIError(400, "clip import must be an object")
        if payload.get("clear") is True:
            return self.store.set_reference_clip(workspace["session_id"], payload)

        def project_path(value: Any, base: Path) -> Path:
            if not isinstance(value, str) or not value:
                raise APIError(400, "clip source needs a project file path")
            path = Path(value).expanduser()
            return self._project_file(str(path if path.is_absolute() else base / path))

        def read_document(path: Path, *, allow_group: bool = False) -> dict[str, Any]:
            limit = MAX_MULTIVIEW_MANIFEST_BYTES if allow_group else MAX_CLIP_MANIFEST_BYTES
            try:
                with path.open("rb") as source:
                    data = source.read(limit + 1)
                if len(data) > limit:
                    raise APIError(400, f"clip manifest exceeds {limit // (1024 * 1024)} MB")
                document = json.loads(data.decode("utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise APIError(400, "clip manifest cannot be read") from exc
            if not isinstance(document, dict):
                raise APIError(400, "clip manifest must be an object")
            if allow_group and "views" not in document and len(data) > MAX_CLIP_MANIFEST_BYTES:
                raise APIError(400, "single-view clip manifest exceeds 2 MB")
            return document

        def attach_cameras(frames: list[dict[str, Any]], spec: dict[str, Any], base: Path, fps: float) -> None:
            if spec.get("camera_manifest_path") is None:
                return
            cameras = read_document(project_path(spec["camera_manifest_path"], base))
            fixed_camera = cameras.get("camera")
            entries = cameras.get("frames", [])
            if fixed_camera is None and (not isinstance(entries, list) or not 1 <= len(entries) <= 600):
                raise APIError(400, "camera manifest needs camera or timed frames")
            timed = []
            if fixed_camera is None:
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise APIError(400, "camera frame must be an object")
                    timed.append((number(entry.get("time_sec"), "camera time_sec"), self.store._normalize_reference_camera(entry.get("camera"))))
                if len({time for time, _ in timed}) != len(timed):
                    raise APIError(400, "camera frame timestamps must be unique")
            for index, frame in enumerate(frames):
                if fixed_camera is not None:
                    camera = self.store._normalize_reference_camera(fixed_camera)
                else:
                    frame_time = number(frame.get("time_sec", index / fps), "frame time_sec")
                    time, original_camera = min(timed, key=lambda item: abs(item[0] - frame_time))
                    if abs(time - frame_time) > 0.5 / fps + 1e-5:
                        raise APIError(400, "camera manifest must cover each sampled timestamp")
                    camera = copy.deepcopy(original_camera)
                width, height = self.store._image_dimensions(frame["data"])
                intrinsics = camera["intrinsics"]
                scale_x, scale_y = width / intrinsics["width"], height / intrinsics["height"]
                if abs(scale_x - scale_y) > 0.01:
                    raise APIError(400, "camera aspect ratio must match sampled frames")
                for key in ("fx", "cx"):
                    intrinsics[key] *= scale_x
                for key in ("fy", "cy"):
                    intrinsics[key] *= scale_y
                intrinsics.update(width=width, height=height)
                frame["camera"] = camera

        def prepare_view(spec: Any, base: Path, depth: int = 0) -> dict[str, Any]:
            if not isinstance(spec, dict) or depth > 4:
                raise APIError(400, "reference view must be a clip source object")
            if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > MAX_CLIP_MANIFEST_BYTES:
                raise APIError(400, "reference view manifest exceeds 2 MB")
            sources = [key for key in ("manifest_path", "video_path", "frames") if key in spec]
            if len(sources) != 1:
                raise APIError(400, "provide manifest_path or video_path or frames for each view")
            if sources[0] == "manifest_path":
                path = project_path(spec["manifest_path"], base)
                document = read_document(path)
                if "views" in document:
                    raise APIError(400, "nested multi-view manifests are not supported")
                metadata = {key: value for key, value in spec.items() if key in {"name", "fps", "duration_sec", "camera_manifest_path"}}
                document = {**document, **metadata}
                document.setdefault("name", path.stem)
                return prepare_view(document, path.parent, depth + 1)
            source_type = "sequence"
            if sources[0] == "video_path":
                source = project_path(spec["video_path"], base)
                fps = fps_value(spec.get("fps", 10))
                frames, duration = sample_video(source, fps)
                name = spec.get("name", source.name)
                source_type = "video"
            else:
                entries = spec["frames"]
                if not isinstance(entries, list) or not 1 <= len(entries) <= 600:
                    raise APIError(400, "clip manifest must contain 1 to 600 frames")
                frames = []
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        raise APIError(400, "each clip frame needs a path")
                    source = project_path(entry["path"], base)
                    name, data = self.store._read_reference_path(str(source))
                    frame = {"name": entry.get("name", name), "data": data}
                    for key in ("time_sec", "camera"):
                        if key in entry:
                            frame[key] = entry[key]
                    frames.append(frame)
                fps = fps_value(spec.get("fps"))
                last_time = number(frames[-1].get("time_sec", (len(frames) - 1) / fps), "frame time_sec")
                duration = spec.get("duration_sec", last_time + 1 / fps)
                name = spec.get("name", "Reference clip")
            attach_cameras(frames, spec, base, fps)
            return prepare_clip(self.store, {"name": clip_name(name), "fps": fps, "duration_sec": duration},
                                local_frames=frames, source_type=source_type, byte_limit=MAX_LOCAL_CLIP_BYTES, write_media=False)

        sources = [key for key in ("manifest_path", "video_path", "frames") if key in payload]
        if len(sources) != 1:
            raise APIError(400, "provide manifest_path or video_path or frames")
        document = None
        base = self.project_dir
        if sources[0] == "manifest_path":
            source = project_path(payload["manifest_path"], base)
            document = read_document(source, allow_group=True)
            base = source.parent
        controls = {key: payload[key] for key in ("append_view", "replace_view_id") if key in payload}
        if document is not None and "views" in document:
            views = document["views"]
            if not isinstance(views, list) or not 1 <= len(views) <= MAX_REFERENCE_VIEWS:
                raise APIError(400, "multi-view manifest must contain 1 to 8 views")
            prepared = [prepare_view(view, base) for view in views]
            if "name" in document:
                controls["group_name"] = clip_name(document["name"])
        else:
            spec = document if document is not None else payload
            overrides = {key: payload[key] for key in ("name", "fps", "duration_sec", "camera_manifest_path") if key in payload}
            # Existing sequence manifests define their own sampling. The MCP
            # fps argument applies to video, while an explicit nested view fps
            # may still override the sequence selected by that view.
            if document is not None and "frames" in document:
                overrides.pop("fps", None)
            spec = {**spec, **overrides}
            if payload.get("view_name") is not None:
                spec["name"] = payload["view_name"]
            if document is not None:
                spec.setdefault("name", source.stem)
            prepared = [prepare_view(spec, base)]
        return self.store._set_prepared_reference_views(workspace["session_id"], controls, prepared)

    def _reference_camera_manifest(self) -> list[tuple[str, dict[str, Any]]]:
        """Load optional filename-prefix camera mapping from the private data dir.

        Each entry describes a fixed calibrated camera; it intentionally does
        not contain a frame-specific alignment image. A new frame must receive
        its own undistorted image through set_reference_cameras.
        """
        path = self.store.data_dir / "reference_cameras.json"
        if not path.exists():
            return []
        try:
            if path.stat().st_size > 1_000_000:
                raise APIError(400, "reference camera manifest exceeds 1 MB")
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise APIError(400, "reference camera manifest cannot be read") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("entries"), list):
            raise APIError(400, "reference camera manifest must have schema_version 1 and entries")
        entries = []
        seen = set()
        for entry in manifest["entries"]:
            if not isinstance(entry, dict):
                raise APIError(400, "reference camera entry must be an object")
            prefix = entry.get("name_prefix")
            if not isinstance(prefix, str) or not 1 <= len(prefix) <= 180 or any(ord(char) < 32 for char in prefix) or "/" in prefix or "\\" in prefix or prefix in seen:
                raise APIError(400, "reference camera name_prefix must be unique short file text")
            seen.add(prefix)
            entries.append((prefix, self.store._normalize_reference_camera(entry.get("camera"))))
        return entries

    def _manifest_camera(self, name: str, image_bytes: bytes, entries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
        matches = [(len(prefix), camera) for prefix, camera in entries if name.startswith(prefix)]
        if not matches:
            return None
        camera = max(matches, key=lambda item: item[0])[1]
        intrinsics = camera["intrinsics"]
        if self.store._image_dimensions(image_bytes) != (intrinsics["width"], intrinsics["height"]):
            raise APIError(400, "reference camera manifest image dimensions do not match")
        return camera

    def apply_reference_camera_manifest(self) -> dict[str, Any]:
        """Attach a project-specific camera manifest to references already imported."""
        from dynamic import reference_views
        workspace = self.ensure()
        entries = self._reference_camera_manifest()
        if not entries:
            raise APIError(404, "reference camera manifest is not installed")
        session_id = workspace["session_id"]
        session = self.store.get_session(session_id)
        updates = []
        references = [(reference, reference["name"]) for reference in session["reference_images"]]
        references += [(frame, f"{view['name']}_{frame['name']}") for view in reference_views(session.get("reference_clip")) for frame in view["frames"]]
        for reference, qualified_name in references:
            data = (self.store.media_dir / reference["url"].rsplit("/", 1)[-1]).read_bytes()
            camera = self._manifest_camera(reference["name"], data, entries)
            if camera is None and qualified_name != reference["name"]:
                camera = self._manifest_camera(qualified_name, data, entries)
            if camera is not None and reference.get("camera") != camera:
                updates.append({"reference_id": reference["id"], "camera": camera})
        if updates:
            return self.store.set_reference_cameras(session_id, updates)
        return {"session_id": session_id, "reference_images": session["reference_images"], "reference_clip": session.get("reference_clip")}

    def set_reference_cameras(self, cameras: Any) -> dict[str, Any]:
        workspace = self.ensure()
        return self.store.set_reference_cameras(workspace["session_id"], cameras)

    def add_reference_data_url(self, session_id: str, name: Any, data_url: Any) -> dict[str, Any]:
        """Browser uploads also receive camera metadata when their names match."""
        camera = None
        entries = self._reference_camera_manifest()
        if entries:
            if not isinstance(name, str):
                raise APIError(400, "name must be a file name")
            data = self.store._decode_image_data_url(data_url, max_bytes=MAX_REFERENCE_BYTES)
            camera = self._manifest_camera(name, data, entries)
        reference = self.store.add_reference(session_id, name, data_url)
        if camera is not None:
            result = self.store.set_reference_cameras(session_id, [{"reference_id": reference["id"], "camera": camera}])
            return next(item for item in result["reference_images"] if item["id"] == reference["id"])
        return reference

    def begin_external_request(self, *, session_id: Any = None, message: Any = None, cursor: Any = None) -> dict[str, Any]:
        if not self.external_review:
            raise APIError(409, "this workspace uses direct Codex delivery")
        workspace = self.ensure()
        if session_id is not None and session_id != workspace["session_id"]:
            raise APIError(409, "review session belongs to another workspace")
        if message is not None and (not isinstance(message, str) or not 1 <= len(message) <= 2000):
            raise APIError(400, "request message must contain 1 to 2000 characters")
        with self.store.lock:
            session = self.store.state["sessions"][workspace["session_id"]]
            count = session.get("feedback_count", 0)
            if cursor is None:
                cursor = count
            if type(cursor) is not int or not 0 <= cursor <= count:
                raise APIError(400, "cursor must refer to an existing feedback position")
            current = self.store.state["workspace"]
            if self.adapter is None:
                current["agent"] = {"status": "external_idle", "turn_id": None, "error": None}
            if message is not None:
                current["request_feedback"] = {"message": message, "object_ids": [], "at": _now(), "scene_revision": self.store.state["scene"]["revision"]}
            self.store._save()
            self.store.workspace_event("external_feedback_requested", {"session_id": workspace["session_id"], "cursor": cursor, "message": message or ""})
            return {"session_id": workspace["session_id"], "next_cursor": cursor, "scene_revision": self.store.state["scene"]["revision"], "delivery_mode": "external", "thread_id": current.get("thread_id")}

    def take_external_feedback(self, session_id: str, cursor: int) -> dict[str, Any]:
        if self.feedback_transport == "mcp_events":
            raise APIError(409, "event feedback is read by ID; this transport has no legacy wait/poll delivery")
        if not self.external_review:
            raise APIError(409, "this workspace uses direct Codex delivery")
        if self.adapter is not None:
            raise APIError(409, "feedback is delivered directly to the bound Codex task")
        workspace = self.ensure()
        if session_id != workspace["session_id"]:
            raise APIError(409, "review session belongs to another workspace")
        with self.store.lock:
            result = self.store.feedback(session_id, cursor)
            if result["items"]:
                ids = {item["feedback_id"] for item in result["items"]}
                for item in self.store.state["workspace"]["queue"]:
                    if item["feedback_id"] in ids and item["status"] == "awaiting_mcp":
                        item["status"] = "returned_to_mcp"
                self.store.state["workspace"]["agent"] = {"status": "external_idle", "turn_id": None, "error": None}
                self.store._save()
                self.store.workspace_event("feedback_returned_to_mcp", {"feedback_ids": sorted(ids)})
            return result

    def confirm_queue(self, feedback_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Keep the adapter used to inspect an uncertain delivery bound until
        # its state transition is saved. Switching in between would inspect
        # the wrong Codex task and could replay a packet there.
        with self._supervisor_lock:
            return self._confirm_queue_bound(feedback_id, payload)

    def _confirm_queue_bound(self, feedback_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        retry_requested = payload.get("retry_uncertain") is True or payload.get("retry_failed") is True
        if retry_requested:
            if self.adapter is None:
                raise APIError(503, "Codex App Server is unavailable")
            with self.store.lock:
                workspace = self.store.state["workspace"]
                item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == feedback_id), None)
                if item is None:
                    raise APIError(404, "queued feedback not found")
                if item.get("feedback_transport") == "mcp_events":
                    raise APIError(409, "event feedback cannot be retried through a Codex task")
                if item["status"] not in {"delivery_uncertain", "failed"}:
                    raise APIError(409, "feedback is not awaiting retry")
                if item.get("target_thread_id") != workspace.get("thread_id"):
                    raise APIError(409, "switch back to this feedback's Codex task before retrying")
                if workspace.get("active_feedback_id") not in {None, feedback_id}:
                    raise APIError(409, "another feedback is active")
            try:
                turn, duplicate = self._matching_turn(feedback_id, item.get("turn_id"))
                if duplicate:
                    raise APIError(409, "multiple Codex turns already contain this feedback ID")
                if turn:
                    self._apply_reconciled_turn(feedback_id, turn)
                    raise APIError(409, "feedback already reached the Codex task; its status was reconciled")
                runtime_status = self.adapter.inspect_thread_status() if hasattr(self.adapter, "inspect_thread_status") else self.adapter.refresh().get("turn_state")
                if runtime_status != "idle":
                    raise APIError(409, "Codex task is active; retry after it becomes idle")
                if hasattr(self.adapter, "release_uncertain"):
                    self.adapter.release_uncertain()
            except APIError:
                raise
            except Exception as exc:
                raise APIError(503, f"cannot reconcile Codex thread before retry: {exc}") from exc
        with self.store.lock:
            workspace = self.store.state["workspace"]
            item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == feedback_id), None)
            if item is None:
                raise APIError(404, "queued feedback not found")
            if item["status"] == "blocked_stale":
                if payload.get("confirm") is True:
                    item["status"] = "queued"
                    item["confirmed_against_revision"] = self.store.state["scene"]["revision"]
                    kind = "stale_feedback_confirmed"
                elif payload.get("confirm") is False:
                    item["status"] = "discarded"
                    kind = "queued_feedback_discarded"
                else:
                    raise APIError(400, "confirm must be true or false")
            elif item["status"] == "delivery_uncertain":
                if payload.get("retry_uncertain") is True:
                    item["status"] = "queued"
                    item["error"] = None
                    item.pop("quarantined_at", None)
                    item.pop("uncertain_since", None)
                    if workspace.get("active_feedback_id") == feedback_id:
                        workspace["active_feedback_id"] = None
                        workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                    kind = "uncertain_feedback_retry_requested"
                elif payload.get("retry_uncertain") is False:
                    item["status"] = "discarded"
                    if workspace.get("active_feedback_id") == feedback_id:
                        workspace["active_feedback_id"] = None
                        workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                    kind = "uncertain_feedback_discarded"
                else:
                    raise APIError(400, "retry_uncertain must be true or false")
            elif item["status"] == "failed" and payload.get("retry_failed") is True:
                item["status"] = "queued"
                item["error"] = None
                item.pop("uncertain_since", None)
                item.pop("quarantined_at", None)
                kind = "failed_feedback_retry_requested"
            else:
                raise APIError(409, "feedback is not awaiting confirmation")
            self.store._save()
            self.store.workspace_event(kind, {"feedback_id": feedback_id})
            result = copy.deepcopy(item)
        self.wake()
        return result

    def wake(self) -> None:
        if not self._started or self.adapter is None:
            return
        with self._worker_lock:
            if self._worker_running:
                return
            self._worker_running = True
            self._worker_thread = threading.Thread(target=self._dispatch, daemon=True, name="workspace-dispatch")
            self._worker_thread.start()

    def _dispatch(self) -> None:
        send_attempted = False
        defer_retry = False
        try:
            if not self.adapter.status().get("connected"):
                if self.external_review:
                    # The supervisor reconnects periodically. Do not claim a
                    # queued packet while the transport is unavailable.
                    return
                try:
                    thread_id = self.adapter.start()
                    self.store.workspace_thread(thread_id)
                    with self.store.lock:
                        if not self.store.state["workspace"].get("active_feedback_id"):
                            self.store.state["workspace"]["agent"] = {"status": "idle", "turn_id": None, "error": None}
                            self.store._save()
                except Exception as exc:
                    self.store.workspace_agent(status="disconnected", error=str(exc)[:500])
                    self.store.workspace_event("disconnected", {"message": str(exc)[:500]})
                    return
            with self.store.lock:
                workspace = self.store.state["workspace"]
                if workspace.get("active_feedback_id"):
                    return
                # The submission already authorizes sending this immutable
                # packet. A scene published while it waits does not invalidate
                # the captured evidence or require another send confirmation.
                item = next((entry for entry in workspace["queue"] if entry["status"] == "queued"
                             and entry.get("feedback_transport") != "mcp_events"
                             and entry.get("target_thread_id") == workspace.get("thread_id")), None)
                if item is None:
                    return
                revision = self.store.state["scene"]["revision"]
                item["status"] = "dispatching"
                item["error"] = None
                workspace["active_feedback_id"] = item["feedback_id"]
                workspace["agent"] = {"status": "running", "turn_id": None, "error": None}
                self.store._save()
                feedback = next(entry for entry in self.store.state["feedback"] if entry["feedback_id"] == item["feedback_id"])
                feedback = copy.deepcopy(feedback)
                feedback["delivery_scene_revision"] = revision
                if item["scene_revision"] != revision:
                    feedback["submitted_from_stale_snapshot"] = True
            text, image_paths = self._turn_input(feedback)
            send_attempted = True
            response = self.adapter.start_turn(text, image_paths, message_id=feedback["feedback_id"])
            with self.store.lock:
                workspace = self.store.state["workspace"]
                current = next(entry for entry in workspace["queue"] if entry["feedback_id"] == feedback["feedback_id"])
                if current["status"] == "dispatching":
                    current["status"] = "running"
                    current["turn_id"] = response.get("turn_id")
                    current["error"] = None
                    current.pop("uncertain_since", None)
                    workspace["agent"] = {"status": "awaiting_approval" if workspace["approvals"] else "running",
                                          "turn_id": response.get("turn_id"), "error": None}
                    self.store._save()
                    self.store.workspace_event("turn_started", {"feedback_id": feedback["feedback_id"], "turn_id": response.get("turn_id")})
        except Exception as exc:
            error = str(exc)[:500]
            not_ready = isinstance(exc, (DeliveryNotReadyError, TurnBusyError, SharedThreadNotIdle))
            rejected = isinstance(exc, DeliveryRejectedError)
            uncertain = not not_ready and send_attempted and not rejected
            defer_retry = not_ready
            with self.store.lock:
                workspace = self.store.state["workspace"]
                active_id = workspace.get("active_feedback_id")
                item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == active_id), None)
                if item:
                    item["status"] = "queued" if not_ready else "delivery_uncertain" if uncertain else "failed"
                    item["error"] = error
                    if uncertain:
                        item.setdefault("uncertain_since", _now())
                workspace["active_feedback_id"] = active_id if uncertain else None
                workspace["agent"] = {"status": "delivery_uncertain" if uncertain else "waiting" if not_ready else "error", "turn_id": None, "error": error}
                self.store._save()
                self.store.workspace_event("delivery_uncertain" if uncertain else "delivery_waiting" if not_ready else "turn_failed", {"feedback_id": active_id, "message": error})
            if not not_ready:
                logging.exception("Could not deliver workspace feedback")
        finally:
            with self._worker_lock:
                self._worker_running = False
            with self.store.lock:
                workspace = self.store.state.get("workspace", {})
                pending = not workspace.get("active_feedback_id") and any(item["status"] == "queued"
                    and item.get("feedback_transport") != "mcp_events"
                    and item.get("target_thread_id") == workspace.get("thread_id") for item in workspace.get("queue", []))
            if pending and not defer_retry and self.adapter.status().get("connected"):
                self.wake()

    def _turn_input(self, feedback: dict[str, Any]) -> tuple[str, list[str]]:
        plan = model_input_plan(feedback, self.store.data_dir)
        manifest = copy.deepcopy(plan["manifest"])
        note = manifest.pop("note", "") or "请结合图像和标记继续重建。"
        lines = ["视觉反馈；项目：" + str(self.project_dir), "用户提示：" + note,
                 "证据：" + json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                 "附件："]
        paths = []
        for index, image in enumerate(plan["images"], 1):
            paths.append(image["path"])
            lines.append(f"{index}. " + image_caption(image))
        lines.append("标记是用户指示，叠图重影不是新增物体。帧号引用从1起，frame_index从0起；时间是片段相对秒，修改时段以提示为准。完整证据按details读取；完成后用workspace_publish_scene发布GLB。")
        if manifest.get("human_pose"):
            lines.append("人体按evidence_kind/keypoint_profile解读；projected_3d不是二维实测，跨机位身份须有证据。")
        if manifest.get("human_pose_edits"):
            lines.append("用$capsule-human-tracking apply-corrections读取corrections_path；仅visible人工点可作可见观测，未改点保留原证据类型。")
        if manifest.get("scene_refs"):
            lines.append("相似场景引用仅供只读类比；需要时读取其snapshot_json_path和glb_paths。只修改当前项目，勿修改来源场景或把引用物体直接当作当前场景对象。")
        return "\n".join(lines), paths

    @staticmethod
    def _update_agent_after_approval(workspace: dict[str, Any]) -> None:
        agent = workspace["agent"]
        if workspace["approvals"]:
            agent["status"] = "awaiting_approval"
        elif workspace.get("active_feedback_id"):
            agent["status"] = "running"
        elif agent.get("status") == "awaiting_approval":
            agent["status"] = "idle"

    def on_adapter_event(self, event: dict[str, Any]) -> None:
        method = event.get("method", "")
        params = event.get("params") or {}
        try:
            if self.external_review and isinstance(params, dict):
                source_thread_id = params.get("threadId") or params.get("thread_id")
                if source_thread_id:
                    with self.store.lock:
                        current_thread_id = self.store.state["workspace"].get("thread_id")
                    if current_thread_id != source_thread_id:
                        return
            if method == "adapter/request_pending":
                approval_id = __import__("uuid").uuid4().hex
                details = params.get("params", {})
                if not isinstance(details, dict):
                    details = {}
                source_thread_id = params.get("source_thread_id") or details.get("threadId") or params.get("thread_id")
                encoded = json.dumps(details, ensure_ascii=False, allow_nan=False)
                too_large = len(encoded.encode("utf-8")) > 64 * 1024
                questions = details.get("questions")
                question_ids = None
                if isinstance(questions, list) and len(questions) <= 100 and all(isinstance(question, dict) and isinstance(question.get("id"), str) and question["id"] for question in questions):
                    candidate_ids = [question["id"] for question in questions]
                    if len(json.dumps(candidate_ids, ensure_ascii=False).encode("utf-8")) <= 4096:
                        question_ids = candidate_ids
                request = {
                    "approval_id": approval_id,
                    "request_id": params.get("request_id"),
                    "source_thread_id": source_thread_id,
                    "kind": params.get("method"),
                    "prompt": encoded[:4000],
                    "details": None if too_large else copy.deepcopy(details),
                    "details_truncated": too_large,
                    "question_ids": question_ids,
                    "at": _now(),
                }
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    workspace["approvals"].append(request)
                    workspace["agent"]["status"] = "awaiting_approval"
                    self.store._save()
                self.store.workspace_event("approval_requested", {"approval_id": approval_id, "kind": request["kind"], "prompt": request["prompt"], "details_truncated": too_large})
            elif method in {"adapter/request_resolved", "serverRequest/resolved"}:
                request_id = params.get("request_id") if method == "adapter/request_resolved" else params.get("requestId")
                source_thread_id = params.get("source_thread_id") or params.get("threadId")
                if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                    return
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    if source_thread_id is None:
                        source_thread_id = workspace.get("thread_id")
                    if not isinstance(source_thread_id, str) or not source_thread_id:
                        return
                    resolved = [entry for entry in workspace["approvals"]
                                if type(entry.get("request_id")) is type(request_id)
                                and entry.get("request_id") == request_id
                                and (entry.get("source_thread_id") or workspace.get("thread_id")) == source_thread_id]
                    if not resolved:
                        return
                    resolved_ids = {entry["approval_id"] for entry in resolved}
                    workspace["approvals"] = [entry for entry in workspace["approvals"] if entry["approval_id"] not in resolved_ids]
                    self._update_agent_after_approval(workspace)
                    self.store._save()
                for entry in resolved:
                    self.store.workspace_event("approval_resolved", {"approval_id": entry["approval_id"], "decision": "resolved_elsewhere"})
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                turn_id = turn.get("id")
                outcome = turn.get("status", "completed")
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    active_id = workspace.get("active_feedback_id")
                    item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == active_id), None)
                    known_turn_id = item.get("turn_id") if item else None
                    if not known_turn_id or not turn_id or known_turn_id != turn_id:
                        self.store.workspace_event("codex_event", {"method": method, "turn_id": turn_id, "status": outcome, "message": "completion did not match a known active turn"})
                        return
                    if item:
                        item["status"] = outcome if outcome in {"completed", "failed", "interrupted"} else "failed"
                        item["turn_id"] = turn_id or item.get("turn_id")
                    workspace["active_feedback_id"] = None
                    workspace["approvals"] = []
                    workspace["agent"] = {"status": "idle", "turn_id": None, "error": None}
                    self.store._save()
                self.store.workspace_event("turn_completed" if outcome == "completed" else "turn_failed", {"feedback_id": active_id, "turn_id": turn_id, "status": outcome})
                self.wake()
            elif method == "turn/started":
                turn = params.get("turn") or {}
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    active_id = workspace.get("active_feedback_id")
                    item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == active_id), None)
                    if item and item["status"] == "dispatching" and turn.get("id"):
                        item["turn_id"] = turn["id"]
                        workspace["agent"]["turn_id"] = turn["id"]
                        self.store._save()
                self.store.workspace_event("codex_event", {"method": method, "turn_id": turn.get("id")})
            elif method == "adapter/disconnected":
                with self.store.lock:
                    workspace = self.store.state["workspace"]
                    active_id = workspace.get("active_feedback_id")
                    if active_id:
                        item = next((entry for entry in workspace["queue"] if entry["feedback_id"] == active_id), None)
                        if item and item["status"] in {"dispatching", "running"}:
                            item["status"] = "delivery_uncertain"
                            item["error"] = "Codex App Server disconnected during a turn; inspect the thread before retrying."
                            item.setdefault("uncertain_since", _now())
                        workspace["agent"] = {"status": "delivery_uncertain", "turn_id": item.get("turn_id") if item else None, "error": item.get("error") if item else None}
                    else:
                        workspace["agent"] = {"status": "disconnected", "turn_id": None, "error": "Codex App Server disconnected"}
                    workspace["approvals"] = []
                    self.store._save()
                self.store.workspace_event("delivery_uncertain" if active_id else "disconnected", {"feedback_id": active_id, "message": "Codex App Server disconnected"})
            elif method == "adapter/reconnected":
                thread_id = params.get("thread_id")
                if thread_id:
                    self.store.workspace_thread(thread_id)
                with self.store.lock:
                    active_id = self.store.state["workspace"].get("active_feedback_id")
                if not active_id:
                    self.store.workspace_agent(status="idle")
                self.store.workspace_event("reconnected", {})
                if not active_id:
                    self.wake()
            elif method == "adapter/empty_thread_recreated":
                thread_id = params.get("thread_id")
                if thread_id:
                    self.store.workspace_thread(thread_id)
            elif method == "item/completed":
                item = params.get("item") if isinstance(params, dict) else None
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    message = item.get("text")
                    if isinstance(message, str) and message:
                        self.store.workspace_event("assistant_message", {"text": message[:30_000]})
                else:
                    summary = {"method": method, "item_type": item.get("type") if isinstance(item, dict) else None}
                    if isinstance(item, dict) and isinstance(item.get("command"), str):
                        summary["command"] = item["command"][:1000]
                    self.store.workspace_event("codex_event", summary)
            elif method == "error":
                self.store.workspace_event("codex_event", {"method": method, "preview": json.dumps(params, ensure_ascii=False)[:4000]})
        except Exception:
            logging.exception("Could not process Codex App Server event")

    def respond_to_approval(self, approval_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.adapter is None:
            raise APIError(503, "Codex App Server is unavailable")
        if not isinstance(payload, dict):
            raise APIError(400, "approval response must be a JSON object")
        with self.store.lock:
            request = next((entry for entry in self.store.state["workspace"]["approvals"] if entry["approval_id"] == approval_id), None)
            if request is None:
                raise APIError(404, "approval request not found")
            request = copy.deepcopy(request)
        decision = payload.get("decision")
        kind = request["kind"]
        details = request.get("details")
        if details is None and not request.get("details_truncated"):
            try:
                details = json.loads(request.get("prompt", ""))
            except (TypeError, ValueError):
                details = {}
        if not isinstance(details, dict):
            details = {}
        supported_kinds = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "mcpServer/elicitation/request",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
        }
        if request.get("details_truncated") and kind not in supported_kinds:
            raise APIError(422, "unsupported request has oversized details; interrupt this turn from the workbench")
        if request.get("details_truncated") and decision not in {"decline", "cancel"}:
            raise APIError(413, "approval details are too large to inspect in the workbench; accepting is unavailable")
        if kind in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            if decision not in {"accept", "acceptForSession", "decline", "cancel"}:
                raise APIError(400, "decision must be accept, acceptForSession, decline or cancel")
            result = {"decision": decision}
        elif kind == "mcpServer/elicitation/request":
            if decision not in {"accept", "decline", "cancel"}:
                raise APIError(400, "decision must be accept, decline or cancel")
            result = {"action": decision}
            if decision == "accept":
                mode = details.get("mode")
                if mode in {"url", "openai/userVerification"}:
                    if mode == "url" and (not isinstance(details.get("url"), str) or not details["url"]):
                        raise APIError(400, "URL elicitation has no usable URL")
                    if "content" in payload:
                        raise APIError(400, "URL elicitation accept must not include form content")
                elif mode in {"form", "openai/form", "openaiForm"}:
                    schema = details.get("requestedSchema")
                    if not isinstance(schema, dict):
                        raise APIError(400, "form elicitation has no requested schema")
                    content = payload.get("content")
                    if content is None and schema.get("type") == "object" and not schema.get("properties") and not schema.get("required"):
                        content = {}
                    if not isinstance(content, dict):
                        raise APIError(400, "accepted elicitation needs form content")
                    self._validate_form_content(schema, content, strict=mode == "form")
                    result["content"] = copy.deepcopy(content)
                else:
                    raise APIError(400, "unsupported elicitation mode")
        elif kind == "item/permissions/requestApproval":
            if decision not in {"accept", "decline", "cancel"}:
                raise APIError(400, "permission decision must be accept, decline or cancel")
            if decision == "accept":
                requested = details.get("permissions")
                if not isinstance(requested, dict):
                    raise APIError(400, "permission request has no profile to inspect")
                submitted = payload.get("permissions", requested)
                if submitted != requested:
                    raise APIError(400, "granted permissions must exactly match the inspected request")
                scope = payload.get("scope", "turn")
                if scope not in {"turn", "session"}:
                    raise APIError(400, "permission scope must be turn or session")
                result = {"permissions": copy.deepcopy(requested), "scope": scope}
            else:
                # The 0.156.1 response has no decline flag. An empty granted
                # profile grants none of the requested extra permissions.
                result = {"permissions": {}, "scope": "turn"}
        elif kind == "item/tool/requestUserInput":
            if decision not in {"accept", "decline", "cancel"}:
                raise APIError(400, "user-input decision must be accept, decline or cancel")
            questions = details.get("questions")
            if isinstance(questions, list) and all(isinstance(question, dict) and isinstance(question.get("id"), str) and question["id"] for question in questions):
                question_ids = [question["id"] for question in questions]
            elif request.get("details_truncated") and decision in {"decline", "cancel"}:
                question_ids = request.get("question_ids")
                if not isinstance(question_ids, list):
                    raise APIError(422, "cannot safely answer oversized user-input request; interrupt this turn from the workbench")
            else:
                raise APIError(400, "user-input request has no valid questions")
            if len(question_ids) != len(set(question_ids)):
                raise APIError(400, "user-input request has duplicate question IDs")
            if decision == "accept":
                answers = payload.get("answers")
                if not isinstance(answers, dict) or set(answers) != set(question_ids):
                    raise APIError(400, "answers must match every requested question ID")
                for question_id, answer in answers.items():
                    if not isinstance(answer, dict) or set(answer) != {"answers"} or not isinstance(answer["answers"], list) or not answer["answers"]:
                        raise APIError(400, f"question {question_id!r} needs answers")
                    if len(answer["answers"]) > 10 or any(not isinstance(value, str) or not 1 <= len(value) <= 4000 for value in answer["answers"]):
                        raise APIError(400, "user-input answers must be short text")
                result = {"answers": copy.deepcopy(answers)}
            else:
                result = {"answers": {question_id: {"answers": []} for question_id in question_ids}}
        else:
            result = payload.get("result")
            if not isinstance(result, dict):
                raise APIError(422, "unsupported request needs a structured result; interrupt this turn from the workbench")
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise APIError(400, "approval response contains invalid JSON values") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise APIError(413, "approval response is too large")
        try:
            self.adapter.respond_to_request(request["request_id"], result)
        except ValueError as exc:
            raise APIError(400, str(exc)) from exc
        with self.store.lock:
            workspace = self.store.state["workspace"]
            workspace["approvals"] = [entry for entry in workspace["approvals"] if entry["approval_id"] != approval_id]
            self._update_agent_after_approval(workspace)
            self.store._save()
        self.store.workspace_event("approval_resolved", {"approval_id": approval_id, "decision": decision or "structured"})
        return {"approval_id": approval_id, "status": "responded"}

    @staticmethod
    def _validate_form_content(schema: dict[str, Any], content: dict[str, Any], *, strict: bool) -> None:
        required = schema.get("required") or []
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise APIError(400, "elicitation schema has invalid required fields")
        if any(key not in content for key in required):
            raise APIError(400, "form content is missing a required field")
        if not strict:
            return
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict) or any(key not in properties for key in content):
            raise APIError(400, "form content contains an unrequested field")
        for key, value in content.items():
            field = properties[key]
            if not isinstance(field, dict):
                raise APIError(400, "elicitation schema has an invalid field")
            kind = field.get("type")
            if kind == "string" and not isinstance(value, str):
                raise APIError(400, f"form field {key!r} must be text")
            if kind == "boolean" and not isinstance(value, bool):
                raise APIError(400, f"form field {key!r} must be true or false")
            if kind in {"integer", "number"} and (isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer" and not isinstance(value, int))):
                raise APIError(400, f"form field {key!r} must be a {kind}")
            if kind == "array" and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
                raise APIError(400, f"form field {key!r} must be a list of text values")
            if "enum" in field and value not in field["enum"]:
                raise APIError(400, f"form field {key!r} is not an offered option")
            if kind == "string" and isinstance(value, str):
                if isinstance(field.get("minLength"), int) and len(value) < field["minLength"]:
                    raise APIError(400, f"form field {key!r} is too short")
                if isinstance(field.get("maxLength"), int) and len(value) > field["maxLength"]:
                    raise APIError(400, f"form field {key!r} is too long")
                choices = field.get("oneOf")
                if isinstance(choices, list) and value not in [option.get("const") for option in choices if isinstance(option, dict)]:
                    raise APIError(400, f"form field {key!r} is not an offered option")
            if kind in {"integer", "number"} and isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = field.get("minimum")
                maximum = field.get("maximum")
                if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
                    raise APIError(400, f"form field {key!r} violates minimum")
                if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
                    raise APIError(400, f"form field {key!r} violates maximum")
            if kind == "array" and isinstance(value, list):
                if isinstance(field.get("minItems"), int) and len(value) < field["minItems"]:
                    raise APIError(400, f"form field {key!r} has too few choices")
                if isinstance(field.get("maxItems"), int) and len(value) > field["maxItems"]:
                    raise APIError(400, f"form field {key!r} has too many choices")
                item_schema = field.get("items")
                if isinstance(item_schema, dict):
                    choices = item_schema.get("enum")
                    if not isinstance(choices, list) and isinstance(item_schema.get("anyOf"), list):
                        choices = [option.get("const") for option in item_schema["anyOf"] if isinstance(option, dict)]
                    if isinstance(choices, list) and any(item not in choices for item in value):
                        raise APIError(400, f"form field {key!r} contains an unoffered choice")

    def interrupt(self) -> dict[str, Any]:
        if self.adapter is None:
            raise APIError(503, "Codex App Server is unavailable")
        with self.store.lock:
            active_id = self.store.state["workspace"].get("active_feedback_id")
            turn_id = self.store.state["workspace"]["agent"].get("turn_id")
        if not active_id:
            raise APIError(409, "there is no active Codex turn")
        response = self.adapter.interrupt(turn_id)
        self.store.workspace_event("interrupt_requested", {"feedback_id": active_id, "turn_id": turn_id})
        return response
