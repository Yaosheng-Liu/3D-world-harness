"""Persistent project catalog; each project keeps its own store and gateway."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack, nullcontext
import copy
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Callable
from urllib.parse import urlencode
import uuid

from core import APIError, SceneStore
from gateway import WorkspaceGateway
from ready_import import discover_ready_instances, host_directory, ReadyInstance
from ready_check import check_ready_instance, check_saved_workspace, ready_check_report


PROJECT_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class ProjectContext:
    project_id: str
    name: str
    store: SceneStore
    gateway: WorkspaceGateway
    data_lock: Any = None

    @property
    def project_dir(self) -> Path:
        return self.gateway.project_dir


class ProjectRegistry:
    def __init__(
        self,
        root: ProjectContext,
        *,
        context_factory: Callable[[dict[str, Any], bool], ProjectContext],
        configure_context: Callable[[ProjectContext], None],
    ):
        self.root = root
        self.path = root.store.data_dir / "project_registry.json"
        self.managed_dir = root.store.data_dir / "projects"
        self._lock = threading.RLock()
        self._import_lock = threading.Lock()
        self.target_binding_lock = threading.RLock()
        self._factory = context_factory
        self._configure = configure_context
        self._contexts = {root.project_id: root}
        self._active_operations: dict[str, int] = {}
        self._closed = False
        self._import_requests: dict[str, str] = {}
        root_record = {
            "project_id": root.project_id, "name": root.name,
            "project_dir": str(root.project_dir), "data_dir": str(root.store.data_dir),
            "creation_status": "ready",
        }
        if self.path.exists():
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("schema_version") != 1 or document.get("default_project_id") != root.project_id:
                raise RuntimeError("project registry does not match the default workspace")
            records = document.get("projects")
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                raise RuntimeError("invalid project registry")
            import_requests = document.get("import_requests", {})
            if not isinstance(import_requests, dict) or any(
                not isinstance(key, str) or not PROJECT_ID.fullmatch(key)
                or not isinstance(value, str) or not Path(value).is_absolute()
                for key, value in import_requests.items()
            ):
                raise RuntimeError("invalid folder import requests in registry")
            self._import_requests = dict(import_requests)
            self._records: dict[str, dict[str, Any]] = {}
            requests: set[str] = set()
            for record in records:
                project_id = record.get("project_id")
                if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id) or project_id in self._records:
                    raise RuntimeError("invalid or duplicate project ID in registry")
                request_id = record.get("request_id")
                if "loaded" in record and type(record["loaded"]) is not bool:
                    raise RuntimeError("invalid project loaded state in registry")
                if request_id is not None:
                    if not isinstance(request_id, str) or not PROJECT_ID.fullmatch(request_id) or request_id in requests:
                        raise RuntimeError("invalid or duplicate creation request in registry")
                    requests.add(request_id)
                    if request_id in self._import_requests:
                        raise RuntimeError("project creation and folder import share a request ID")
                self._records[project_id] = dict(record)
            saved_root = self._records.get(root.project_id)
            if saved_root is None or any(saved_root.get(key) != root_record[key] for key in ("project_dir", "data_dir")):
                raise RuntimeError("default project directories changed in registry")
            if saved_root.get("loaded") is False:
                raise RuntimeError("default project cannot be unloaded")
            root.name = saved_root["name"]
        else:
            self._records = {root.project_id: root_record}
            self._save()
        self._configure(root)
        self._install_binding_guard(root)
        try:
            for project_id, record in self._records.items():
                if project_id == root.project_id:
                    continue
                if record.get("loaded") is False:
                    continue
                if record.get("kind") != "imported":
                    self._validate_managed_paths(record)
                context = None
                try:
                    if record.get("kind") == "imported":
                        self._validate_managed_paths(record)
                    context = self._factory(record, False)
                    self._contexts[project_id] = context
                    self._configure(context)
                    self._install_binding_guard(context)
                    if record.get("kind") == "imported":
                        self._start_imported_context(context, restored=True)
                    else:
                        context.gateway.start()
                except Exception as exc:
                    # A partially created or unavailable child must stay visible
                    # without replacing the existing default workspace.
                    record["creation_status"] = "unavailable"
                    record["creation_error"] = str(exc)[:500]
                    record["creation_http_status"] = 503
                    logging.exception("Could not restore reconstruction project %s", project_id)
                    self._contexts.pop(project_id, None)
                    if context is not None:
                        try:
                            context.gateway.close()
                        except Exception:
                            logging.exception("Could not close unavailable reconstruction project %s", project_id)
                        finally:
                            if context.data_lock is not None:
                                try:
                                    context.data_lock.close()
                                except Exception:
                                    logging.exception("Could not release unavailable project lock %s", project_id)
                    continue
                if record.get("creation_status") == "creating":
                    record["creation_status"] = "uncertain"
                    record["creation_error"] = "project creation was interrupted; inspect its Codex task before creating another"
                    record["creation_http_status"] = 503
                self._refresh_creation(context)
            self._save()
        except BaseException:
            self.close()
            raise

    def _save(self) -> None:
        document = {"schema_version": 1, "default_project_id": self.root.project_id,
                    "projects": list(self._records.values()), "import_requests": self._import_requests}
        descriptor, temporary = tempfile.mkstemp(prefix="projects-", suffix=".json", dir=self.root.store.data_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _validate_managed_paths(self, record: dict[str, Any]) -> None:
        base = self.managed_dir / record["project_id"]
        for key, leaf in (("project_dir", "workspace"), ("data_dir", "data")):
            if key == "project_dir" and record.get("kind") == "imported":
                source = record.get("import_source")
                project = record.get("project_dir")
                if (not isinstance(project, str) or not Path(project).is_absolute()
                    or str(Path(project).resolve()) != project or not isinstance(source, dict)
                    or source.get("root") != project or source.get("read_only") is not True):
                    raise RuntimeError("imported project must use its canonical read-only source directory")
                continue
            expected = base / leaf
            if record.get(key) != str(expected) or expected.resolve() != expected:
                raise RuntimeError("managed project directories must remain inside the registry data directory")
        if not isinstance(record.get("name"), str) or not record["name"].strip():
            raise RuntimeError("managed project has no name")

    def _install_binding_guard(self, context: ProjectContext) -> None:
        context.gateway.target_binding_lock = self.target_binding_lock
        context.gateway.target_validator = lambda thread_id: self.validate_target(context.project_id, thread_id)

    def validate_target(self, project_id: str, thread_id: str) -> None:
        # Called while the shared binding lock is held. Do not take the catalog
        # lock: creation holds it while waiting for a task binding to complete.
        for record in tuple(self._records.values()):
            if record["project_id"] != project_id and (
                thread_id in (record.get("thread_id"), record.get("created_thread_id"))
                or thread_id in record.get("created_thread_ids", [])
            ):
                raise APIError(409, "Codex task belongs to another reconstruction project")
        for context in tuple(self._contexts.values()):
            if context.project_id == project_id:
                continue
            with context.store.lock:
                workspace = context.store.state.get("workspace", {})
                claimed = workspace.get("thread_id") == thread_id or thread_id in workspace.get("created_thread_ids", [])
            if claimed or self._records[context.project_id].get("created_thread_id") == thread_id:
                raise APIError(409, "Codex task belongs to another reconstruction project")

    def contexts(self) -> tuple[ProjectContext, ...]:
        with self._lock:
            return tuple(self._contexts.values())

    def get(self, project_id: str) -> ProjectContext:
        with self._lock:
            if self._closed:
                raise APIError(503, "reconstruction service is closing")
            record = self._records.get(project_id)
            if record is not None and record.get("loaded") is False:
                raise APIError(404, "reconstruction scene is unloaded; import its folder to open it again")
            context = self._contexts.get(project_id)
            if context is None:
                if record is not None:
                    raise APIError(503, record.get("creation_error", "reconstruction project is unavailable"),
                                   detail={"project": self._unavailable_metadata(record)})
                raise APIError(404, "reconstruction project not found")
            return context

    @contextmanager
    def operation(self, context: ProjectContext):
        """Lease a routed context so unloading cannot race an in-flight request.

        A handler may have selected its context before another handler unloads
        it. Verify identity while reserving the operation, then let catalog
        operations proceed without holding the catalog lock for file I/O.
        Unload refuses a leased context instead of waiting, avoiding a lock
        cycle with a folder import submitted through that context.
        """
        with self._lock:
            if self._closed:
                raise APIError(503, "reconstruction service is closing")
            if self._contexts.get(context.project_id) is not context:
                raise APIError(404, "reconstruction scene is unloaded or unavailable")
            self._active_operations[context.project_id] = self._active_operations.get(context.project_id, 0) + 1
        try:
            yield context
        finally:
            with self._lock:
                remaining = self._active_operations[context.project_id] - 1
                if remaining:
                    self._active_operations[context.project_id] = remaining
                else:
                    self._active_operations.pop(context.project_id, None)

    def by_control_token(self, token: str) -> ProjectContext:
        for context in self.contexts():
            if hmac.compare_digest(token, context.store.control_token):
                return context
        raise APIError(403, "invalid local MCP control key")

    def accepts_browser_token(self, token: str) -> bool:
        return any(hmac.compare_digest(token, context.store.browser_token) for context in self.contexts())

    def _refresh_creation(self, context: ProjectContext) -> None:
        record = self._records[context.project_id]
        if "workspace" not in context.store.state:
            return
        workspace = context.store.workspace()
        record.update({key: workspace.get(key) for key in ("session_id", "thread_id", "scene_revision")})
        record["created_thread_ids"] = list(workspace.get("created_thread_ids", []))
        if (workspace.get("thread_id") or record.get("kind") == "imported") and record.get("creation_status") != "ready":
            record["creation_status"] = "ready"
            if workspace.get("thread_id"):
                record["created_thread_id"] = workspace["thread_id"]
            record.pop("creation_error", None)
            record.pop("creation_http_status", None)

    def metadata(self, context: ProjectContext) -> dict[str, Any]:
        context.gateway.ensure()
        workspace = context.store.workspace()
        record = self._records[context.project_id]
        result = {
            "project_id": context.project_id, "name": context.name,
            "project_dir": str(context.project_dir), "session_id": workspace["session_id"],
            "thread_id": workspace.get("thread_id"), "scene_revision": workspace["scene_revision"],
            "creation_status": record.get("creation_status", "ready"),
            "can_unload": context is not self.root,
            "url": f"/p/{context.project_id}/?{urlencode({'session_id': workspace['session_id']})}",
        }
        with context.store.lock:
            session = context.store.state["sessions"].get(workspace["session_id"], {})
            result["can_reference_scene"] = (not getattr(context.gateway, "blank_workbench", False)
                and record.get("creation_status", "ready") == "ready"
                and bool(context.store.state["scene"].get("objects") or session.get("reference_images") or session.get("reference_clip")))
        for key in ("creation_error", "created_thread_id", "request_id"):
            if record.get(key):
                result[key] = record[key]
        if record.get("kind") == "imported":
            result["kind"] = "imported"
            result["import_source"] = copy.deepcopy(record["import_source"])
        return result

    def _unavailable_metadata(self, record: dict[str, Any]) -> dict[str, Any]:
        result = {key: record.get(key) for key in (
            "project_id", "name", "project_dir", "session_id", "thread_id", "scene_revision", "creation_status"
        )}
        query = urlencode({"session_id": record["session_id"]}) if record.get("session_id") else ""
        result["url"] = f"/p/{record['project_id']}/" + ("?" + query if query else "")
        result["can_unload"] = record["project_id"] != self.root.project_id
        result["can_reference_scene"] = False
        for key in ("creation_error", "created_thread_id", "request_id"):
            if record.get(key):
                result[key] = record[key]
        if record.get("kind") == "imported":
            result["kind"] = "imported"
            result["import_source"] = copy.deepcopy(record["import_source"])
        return result

    def list(self, current_project_id: str) -> dict[str, Any]:
        with self._lock:
            before = json.dumps(self._records, sort_keys=True)
            for context in self._contexts.values():
                self._refresh_creation(context)
            if before != json.dumps(self._records, sort_keys=True):
                self._save()
            return {"projects": [self.metadata(self._contexts[project_id]) if project_id in self._contexts
                                 else self._unavailable_metadata(record) for project_id, record in self._records.items()
                                 if record.get("loaded") is not False],
                    "current_project_id": current_project_id, "project_unload_supported": True}

    def capture_scene_reference(self, receiver: ProjectContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Lease both contexts, freeze source evidence, then copy to receiver."""
        if not isinstance(payload, dict) or set(payload) != {"project_id"}:
            raise APIError(400, "scene reference capture accepts only project_id")
        source_id = payload["project_id"]
        if not isinstance(source_id, str) or not PROJECT_ID.fullmatch(source_id):
            raise APIError(400, "source project_id must be a lowercase UUID hex value")
        if source_id == receiver.project_id:
            raise APIError(400, "choose another scene as the reference")
        if getattr(receiver.gateway, "blank_workbench", False):
            raise APIError(409, "select a reconstruction scene before citing a scene reference")
        source = self.get(source_id)
        if getattr(source.gateway, "blank_workbench", False):
            raise APIError(400, "the blank home page cannot be a scene reference")
        with self.operation(receiver), self.operation(source):
            with self._lock:
                source_record = copy.deepcopy(self._records[source_id])
            if source_record.get("creation_status", "ready") != "ready":
                raise APIError(409, "source scene is not available for referencing")
            with source.store.lock:
                workspace = source.store.state.get("workspace", {})
                source_session = workspace.get("session_id")
                session = source.store.state["sessions"].get(source_session, {})
                scene = copy.deepcopy(source.store.state["scene"])
                selected = (session.get("reference_images") or [None])[0]
                clip = session.get("reference_clip")
                view = None
                if selected is None and clip:
                    view = clip
                    selected = (clip.get("frames") or [None])[0]
                image = None
                if selected:
                    image = {key: copy.deepcopy(selected[key]) for key in ("id", "name", "url", "camera", "time_sec", "frame_index") if key in selected}
                    if view is not None:
                        image.update(view_id=view["clip_id"], view_name=view["name"])
                evidence = {"name": source.name, "source_project_id": source_id,
                            "source_session_id": source_session, "source_scene_revision": scene["revision"],
                            "source_project_dir": str(source.project_dir), "scene": scene}
                if image:
                    evidence["source_reference_image"] = image
            blend = source_record.get("import_source", {}).get("blend")
            if isinstance(blend, str):
                path = Path(blend).resolve()
                if path.is_file() and path.suffix.lower() == ".blend" and path.is_relative_to(source.project_dir):
                    evidence["source_blend_path"] = str(path)
            from scene_references import SceneReferences
            return SceneReferences(receiver.store).capture(source.store, evidence)

    @staticmethod
    def _check_unload_workspace(workspace: dict[str, Any], gateway: WorkspaceGateway | None = None) -> None:
        if workspace.get("active_feedback_id") or workspace.get("approvals"):
            raise APIError(409, "请先完成正在处理的反馈或审批，再卸载场景。")
        if workspace.get("agent", {}).get("status") in {"running", "dispatching", "awaiting_approval", "delivery_uncertain", "starting", "creating"}:
            raise APIError(409, "Codex 正在处理这个场景，请等处理完成后再卸载。")
        for item in workspace.get("queue", []):
            status = item.get("status")
            if item.get("feedback_transport") == "mcp_events" and gateway is not None and gateway.mcp_events is not None:
                # Event delivery receipts are stored outside SceneStore. A
                # delivered receipt permits closing this local viewer; it
                # does not claim that the remote model turn has completed.
                status = gateway.mcp_events.feedback_status(item["feedback_id"])["status"]
            returned = status == "returned_to_mcp" and (gateway is None or gateway.adapter is None)
            if not returned and status not in {"completed", "failed", "interrupted", "discarded", "event_delivered"}:
                raise APIError(409, "这个场景还有等待发送或确认的反馈，请处理完后再卸载。")

    def unload(self, project_id: str) -> dict[str, Any]:
        """Detach a child without deleting its source, data, or Codex task."""
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise APIError(400, "project_id must be a 32-character lowercase UUID hex value")
        # Import and reactivation hold this same lock through data-lock release.
        # A repeated import can therefore never open a half-closed context.
        with self._import_lock:
            with self._lock:
                if self._closed:
                    raise APIError(503, "reconstruction service is closing")
                record = self._records.get(project_id)
                if record is None:
                    raise APIError(404, "reconstruction project not found")
                if project_id == self.root.project_id:
                    raise APIError(409, "默认场景不能卸载。")
                if record.get("loaded") is False:
                    return {"unloaded_project_id": project_id, "fallback": self.metadata(self.root)}
                if record.get("creation_status") in {"creating", "uncertain"}:
                    raise APIError(409, "这个场景的任务仍在创建或等待确认，请先处理完成。")
                context = self._contexts.get(project_id)
                if self._active_operations.get(project_id):
                    raise APIError(409, "场景正在读取或更新，请稍后再卸载。")
            with ExitStack() as locks:
                if context is not None:
                    gateway = context.gateway
                    for lock in (gateway._supervisor_lock, gateway._adapter_event_lock, gateway._worker_lock,
                                 gateway.target_binding_lock):
                        if not lock.acquire(blocking=False):
                            raise APIError(409, "场景正在更新或连接 Codex，请稍后再卸载。")
                        locks.callback(lock.release)
                    if gateway._worker_running:
                        raise APIError(409, "反馈正在发送，请等发送完成后再卸载。")
                    adapter = gateway.adapter
                    if adapter is not None:
                        status = adapter.status()
                        if status.get("turn_state") in {"active", "running", "starting", "uncertain"} or status.get("pending_requests"):
                            raise APIError(409, "Codex 正在处理这个场景，请等处理完成后再卸载。")
                        if status.get("connected") and hasattr(adapter, "inspect_thread_status"):
                            try:
                                if adapter.inspect_thread_status() != "idle":
                                    raise APIError(409, "Codex 正在处理这个场景，请等处理完成后再卸载。")
                            except APIError:
                                raise
                            except Exception as exc:
                                raise APIError(503, "暂时无法确认 Codex 任务状态，请稍后再卸载。") from exc
                else:
                    # An unavailable child has no local runtime to stop. Keep
                    # unresolved durable feedback discoverable in the catalog.
                    state_path = Path(record["data_dir"]) / "state.json"
                    if state_path.is_file():
                        try:
                            state = json.loads(state_path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            state = {}
                        self._check_unload_workspace(state.get("workspace", {}))
                with self._lock, context.store.lock if context is not None else nullcontext():
                    if self._closed:
                        raise APIError(503, "reconstruction service is closing")
                    if self._active_operations.get(project_id):
                        raise APIError(409, "场景正在读取或更新，请稍后再卸载。")
                    if context is not None:
                        self._check_unload_workspace(context.store.state.get("workspace", {}), context.gateway)
                    fallback = self.metadata(self.root)
                    previous = copy.deepcopy(record)
                    try:
                        if context is not None:
                            self._refresh_creation(context)
                        record["loaded"] = False
                        self._save()
                    except BaseException:
                        record.clear()
                        record.update(previous)
                        raise
                    self._contexts.pop(project_id, None)
                    if context is not None:
                        context.gateway._started = False
                        context.gateway._supervisor_stop.set()
                        context.gateway._adapter_token = object()
            # Joining workers and releasing the store lock happens after the
            # catalog lock is released. Original assets and persisted tasks
            # remain intact, including the old browser/control credentials.
            self._close_import_context(context)
            return {"unloaded_project_id": project_id, "fallback": fallback}

    def authorize_unload_retry(self, project_id: str, browser_token: str) -> bool:
        """Authorize only an idempotent DELETE at its now-unloaded URL.

        Read the preserved credential file without opening a context, adapter,
        or worker. Normal routing and all other APIs continue to reject it.
        """
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id) or not isinstance(browser_token, str):
            return False
        with self._lock:
            record = self._records.get(project_id)
            if self._closed or record is None or record.get("loaded") is not False or project_id == self.root.project_id:
                return False
            data = self.managed_dir / project_id / "data"
            if record.get("data_dir") != str(data) or data.resolve() != data:
                return False
            token_path = data / "browser_token"
            if token_path.is_symlink():
                return False
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                return False
            return bool(token) and hmac.compare_digest(browser_token.encode("utf-8"), token.encode("utf-8"))

    @staticmethod
    def _creation_spec(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if set(payload) - {"name", "model", "reasoning_effort", "permission_mode", "request_id"}:
            raise APIError(400, "project creation accepts name and Codex settings, not host directory paths")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not PROJECT_ID.fullmatch(request_id):
            raise APIError(400, "request_id must be a 32-character lowercase UUID hex value")
        name, model = payload.get("name"), payload.get("model")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(ch) < 32 for ch in name):
            raise APIError(400, "scene name must contain 1 to 120 printable characters")
        if not isinstance(model, str) or not model.strip():
            raise APIError(400, "select a Codex model")
        permission = payload.get("permission_mode", "workspace_write")
        if not isinstance(permission, str) or permission not in {"full_access", "workspace_write", "read_only"}:
            raise APIError(400, "permission_mode must be full_access, workspace_write, or read_only")
        effort = payload.get("reasoning_effort")
        if effort is not None and not isinstance(effort, str):
            raise APIError(400, "reasoning_effort must be a supported value")
        return request_id, {"name": name.strip(), "model": model.strip(), "reasoning_effort": effort,
                            "permission_mode": permission}

    def _creation_result(self, context: ProjectContext) -> dict[str, Any]:
        record = self._records[context.project_id]
        workspace = context.gateway.state(include_capability=True)
        if hasattr(context.gateway, "browser_url"):
            workspace["browser_url"] = context.gateway.browser_url(workspace["session_id"])
        result = {"project": self.metadata(context), "workspace": workspace}
        if record.get("creation_status") in {"failed", "uncertain", "creating", "unavailable"}:
            raise APIError(record.get("creation_http_status", 503), record.get("creation_error", "project creation is not confirmed"), detail=result)
        return result

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id, spec = self._creation_spec(payload)
        with self._lock:
            if self._closed:
                raise APIError(503, "reconstruction service is closing")
            if request_id in self._import_requests:
                raise APIError(409, "request_id was already used for folder import")
            for record in self._records.values():
                if record.get("request_id") == request_id:
                    if record.get("creation_spec") != spec:
                        raise APIError(409, "request_id was already used with different project settings")
                    context = self._contexts.get(record["project_id"])
                    if context is None:
                        raise APIError(record.get("creation_http_status", 503), record.get("creation_error", "project creation is not confirmed"),
                                       detail={"project": self._unavailable_metadata(record)})
                    self._refresh_creation(context)
                    self._save()
                    return self._creation_result(context)
            if not self.root.gateway.external_review:
                catalog = self.root.gateway.list_models()
                selected = next((item for item in catalog["models"] if item["model"] == spec["model"]), None)
                if selected is None:
                    raise APIError(400, "selected model is not available with image input")
                if spec["reasoning_effort"] is not None and spec["reasoning_effort"] not in selected["supported_reasoning_efforts"]:
                    raise APIError(400, "reasoning_effort is not supported by the selected model")
            project_id = uuid.uuid4().hex
            base = self.managed_dir / project_id
            record = {"project_id": project_id, "name": spec["name"],
                      "project_dir": str(base / "workspace"), "data_dir": str(base / "data"),
                      "creation_status": "creating", "request_id": request_id, "creation_spec": spec}
            self._validate_managed_paths(record)
            self._records[project_id] = record
            self._save()  # Persist intent before any task/start request.
            context: ProjectContext | None = None
            try:
                context = self._factory(record, True)
                self._contexts[project_id] = context
                self._configure(context)
                self._install_binding_guard(context)
                self._refresh_creation(context)
                self._save()
                context.gateway.start()
                if context.gateway.external_review:
                    result = context.gateway.create_target(spec["model"], reasoning_effort=spec["reasoning_effort"],
                                                           permission_mode=spec["permission_mode"], title=spec["name"])
                    thread_id = result["thread_id"]
                else:
                    workspace = context.store.workspace()
                    thread_id = workspace.get("thread_id")
                    if not thread_id or workspace["agent"]["status"] == "disconnected":
                        raise APIError(503, workspace["agent"].get("error") or "cannot create Codex CLI session")
                record["created_thread_id"] = thread_id
                record["creation_status"] = "ready"
                self._refresh_creation(context)
                if context.gateway.external_review and context.gateway._supervisor_thread is None:
                    context.gateway.start()
                self._save()
                return self._creation_result(context)
            except Exception as exc:
                record["creation_status"] = "uncertain" if "uncertain" in str(exc).lower() else "failed"
                record["creation_error"] = exc.message if isinstance(exc, APIError) else str(exc)[:500]
                record["creation_http_status"] = exc.status if isinstance(exc, APIError) else 503
                if isinstance(exc, APIError) and isinstance(exc.detail, dict) and exc.detail.get("thread_id"):
                    record["created_thread_id"] = exc.detail["thread_id"]
                if context is not None:
                    workspace = context.store.workspace()
                    record.update({key: workspace.get(key) for key in ("session_id", "thread_id", "scene_revision")})
                    record["created_thread_ids"] = list(workspace.get("created_thread_ids", []))
                    if workspace.get("created_thread_ids") and not record.get("created_thread_id"):
                        record["created_thread_id"] = workspace["created_thread_ids"][-1]
                self._save()
                if context is not None:
                    return self._creation_result(context)
                raise APIError(record["creation_http_status"], record["creation_error"],
                               detail={"project": self._unavailable_metadata(record)}) from exc

    @staticmethod
    def _start_imported_context(context: ProjectContext, *, restored: bool = False) -> None:
        # Event delivery needs the local worker. An existing task may restore
        # after a user explicitly bound it; a fresh import never owns an adapter.
        if context.gateway.adapter is not None:
            workspace = context.gateway.ensure()
            target = workspace.get("thread_id")
            if not (restored and context.gateway.external_review and target
                    and getattr(context.gateway.adapter, "thread_id", None) == target):
                raise RuntimeError("imported scene unexpectedly has a Codex adapter")
        context.gateway.desktop_seed_thread_id = None
        if context.gateway.external_review or context.gateway.feedback_transport == "mcp_events":
            context.gateway.start()

    def _source_record(self, source: Path) -> dict[str, Any] | None:
        for record in self._records.values():
            if record["project_dir"] == str(source):
                return record
        return None

    @staticmethod
    def _close_import_context(context: ProjectContext | None) -> None:
        if context is None:
            return
        try:
            context.gateway.close()
        except Exception:
            logging.exception("Could not close scene context %s", context.project_id)
        finally:
            if context.data_lock is not None:
                try:
                    context.data_lock.close()
                except Exception:
                    logging.exception("Could not release scene data lock %s", context.project_id)

    def _reactivate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Restore preserved data without copying sources or creating a task.

        The caller holds _import_lock through factory, publication, and cleanup,
        the same lifecycle boundary used by unload and initial folder import.
        """
        project_id = record["project_id"]
        self._validate_managed_paths(record)
        context = None
        try:
            context = self._factory(copy.deepcopy(record), False)
            self._configure(context)
            self._install_binding_guard(context)
            if (context.project_id != project_id or str(context.project_dir) != record["project_dir"]
                    or str(context.store.data_dir) != record["data_dir"]):
                raise RuntimeError("restored scene must use its preserved context and data directory")
            if record.get("kind") == "imported":
                self._start_imported_context(context, restored=True)
            else:
                context.gateway.start()
            with self._lock:
                if self._closed:
                    raise APIError(503, "reconstruction service is closing")
                previous = copy.deepcopy(record)
                try:
                    record["loaded"] = True
                    self._contexts[project_id] = context
                    self._refresh_creation(context)
                    result = self.metadata(context)
                    self._save()
                except BaseException:
                    record.clear()
                    record.update(previous)
                    self._contexts.pop(project_id, None)
                    raise
            return result
        except BaseException:
            self._close_import_context(context)
            raise

    def _import_instance(self, instance: ReadyInstance) -> tuple[dict[str, Any], bool]:
        with self._lock:
            if self._closed:
                raise APIError(503, "reconstruction service is closing")
            existing = self._source_record(instance.root)
            if existing is not None:
                if existing.get("loaded") is False:
                    context = None
                else:
                    context = self._contexts.get(existing["project_id"])
                    if context is None:
                        raise APIError(503, existing.get("creation_error", "registered source is unavailable"))
                    return self.metadata(context), False
        if existing is not None:
            return self._reactivate_record(existing), True
        project_id = uuid.uuid5(uuid.NAMESPACE_URL, "scene-feedback-import:" + str(instance.root)).hex
        base = self.managed_dir / project_id
        record = {"project_id": project_id, "name": instance.name, "kind": "imported",
                  "project_dir": str(instance.root), "data_dir": str(base / "data"),
                  "creation_status": "ready", "import_source": instance.provenance()}
        self._validate_managed_paths(record)
        with self._lock:
            if project_id in self._records:
                raise APIError(409, "imported source conflicts with an existing project ID")
        # Claim only a fresh managed directory. An orphan or externally supplied
        # data directory is never reused for its credentials or old task state.
        self.managed_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            base.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise APIError(409, "managed import data already exists; inspect it before importing this source") from exc
        context = None
        try:
            context = self._factory(record, True)
            self._configure(context)
            self._install_binding_guard(context)
            context.gateway.desktop_seed_thread_id = None
            workspace = context.gateway.ensure()
            if (context.project_id != project_id or context.project_dir != instance.root
                or context.store.data_dir != base / "data" or workspace.get("project_id") != project_id
                or workspace.get("thread_id") or workspace.get("created_thread_ids")
                or workspace.get("queue") or context.store.scene().get("objects")):
                raise RuntimeError("folder import requires a fresh isolated scene context without Codex activity")
            if context.gateway.adapter is not None:
                raise RuntimeError("folder import cannot use a Codex adapter")
            if instance.manifest is not None:
                clip_payload = {"manifest_path": str(instance.manifest)}
                if instance.camera_manifest is not None:
                    clip_payload["camera_manifest_path"] = str(instance.camera_manifest)
                context.gateway.set_reference_clip_paths(clip_payload)
            if instance.reference_images:
                context.gateway.add_reference_paths([str(path) for path in instance.reference_images])
            # Recheck the GLB immediately before copying, since its source may
            # have changed while a large reference group was being imported.
            glb = instance.glb.resolve(strict=True)
            if not glb.is_relative_to(instance.root) or not glb.is_file():
                raise APIError(400, "ready scene files must remain inside their instance directory")
            model = context.store.import_model(str(glb), name=instance.name)
            if instance.world_up is not None:
                up_axis = instance.world_up.lower()
                if up_axis not in {"y", "z"}:
                    raise APIError(400, "ready scene world_up must be Y or Z")
                context.store.update_scene(model["scene_revision"], [{
                    "op": "update", "object_id": model["object"]["id"],
                    "fields": {"metadata": {**model["object"].get("metadata", {}), "up_axis": up_axis}},
                }])
            self._start_imported_context(context)
            with self._lock:
                if self._closed:
                    raise APIError(503, "reconstruction service is closing")
                # Publication follows successful validation/copies, so failed
                # imports do not leave catalog entries that look like projects.
                self._records[project_id] = record
                self._contexts[project_id] = context
                try:
                    self._refresh_creation(context)
                    metadata = self.metadata(context)
                    self._save()
                except BaseException:
                    self._records.pop(project_id, None)
                    self._contexts.pop(project_id, None)
                    raise
            return metadata, True
        except BaseException:
            self._close_import_context(context)
            # This method created base exclusively and only deletes its own
            # managed data; the original instance and editable blend stay intact.
            if base.resolve() == base and base.is_dir():
                shutil.rmtree(base)
            raise

    def import_folder(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) - {"path", "request_id"}:
            raise APIError(400, "folder import accepts path and request_id")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not PROJECT_ID.fullmatch(request_id):
            raise APIError(400, "request_id must be a 32-character lowercase UUID hex value")
        folder = host_directory(payload.get("path"))
        # Keep the catalog lock out of image/model copying, allowing the active
        # scene and project polling to continue while a batch is imported.
        with self._import_lock:
            with self._lock:
                if self._closed:
                    raise APIError(503, "reconstruction service is closing")
                previous = self._import_requests.get(request_id)
                if previous is not None and previous != str(folder):
                    raise APIError(409, "request_id was already used with a different folder")
                if any(record.get("request_id") == request_id for record in self._records.values()):
                    raise APIError(409, "request_id was already used for project creation")
                if previous is None:
                    self._import_requests[request_id] = str(folder)
                    try:
                        self._save()
                    except BaseException:
                        self._import_requests.pop(request_id, None)
                        raise
            discovery = discover_ready_instances(str(folder))
            projects, imported = [], []
            checked = []
            skipped, errors = list(discovery.skipped), list(discovery.errors)
            for instance in discovery.instances:
                check = None
                try:
                    with self._lock:
                        existing = self._source_record(instance.root)
                        context = self._contexts.get(existing["project_id"]) if existing is not None else None
                    if existing is not None:
                        if existing["project_id"] != self.root.project_id:
                            self._validate_managed_paths(existing)
                        state = None
                        if context is not None:
                            with context.store.lock:
                                state = copy.deepcopy({key: context.store.state.get(key) for key in ("scene", "workspace", "sessions")})
                        check = check_saved_workspace(existing["name"], instance.root, instance.format,
                                                      Path(existing["data_dir"]), state)
                    else:
                        check = check_ready_instance(instance)
                    checked.append(check)
                    if not check["can_import"]:
                        problems = [item["message"] for item in check["checks"] if item["status"] == "error"]
                        errors.append({"path": str(instance.root), "error": "; ".join(problems), "code": "ready_check_failed"})
                        continue
                    metadata, created = self._import_instance(instance)
                    projects.append(metadata)
                    item = {"path": str(instance.root), "project_id": metadata["project_id"], "name": metadata["name"]}
                    if created:
                        imported.append(item)
                    else:
                        skipped.append({**item, "reason": "source directory is already registered"})
                except Exception as exc:
                    logging.warning("Could not import ready scene %s: %s", instance.root, exc)
                    message = exc.message if isinstance(exc, APIError) else str(exc)[:500]
                    errors.append({"path": str(instance.root), "error": message})
                    if check is None:
                        check = {"name": instance.name, "path": str(instance.root), "format": instance.format,
                                 "checks": [], "metrics": {}}
                        checked.append(check)
                    check.update(status="blocked", can_import=False)
                    check["checks"].append({"status": "error", "code": "import_failed", "message": "导入失败：" + message})
            report = ready_check_report(discovery, checked)
            for error in report["errors"]:
                if not any(previous["path"] == error["path"] for previous in errors):
                    errors.append(error)
            return {"path": str(folder), "request_id": request_id, "projects": projects,
                    "imported": imported, "skipped": skipped, "errors": errors,
                    "ready_check": report,
                    "truncated": discovery.truncated,
                    "counters": {"imported": len(imported), "skipped": len(skipped), "errors": len(errors),
                                 "candidates": discovery.candidates, "directories_scanned": discovery.directories_scanned,
                                 "entries_scanned": discovery.entries_scanned}}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            contexts = tuple(self._contexts.values())
        first_error: BaseException | None = None
        for context in reversed(contexts):
            try:
                context.gateway.close()
            except BaseException as exc:
                first_error = first_error or exc
            finally:
                if context.data_lock is not None:
                    try:
                        context.data_lock.close()
                    except BaseException as exc:
                        first_error = first_error or exc
        if first_error is not None:
            raise first_error
