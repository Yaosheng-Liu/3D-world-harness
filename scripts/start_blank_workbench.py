"""Start a persistent empty workbench without creating a Codex task."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from server import make_server


WORKBENCH_NAME = "空白工作台"
DEFAULT_BASE_DIR = Path.home() / ".local" / "share" / "scene-feedback" / "blank-workbench"
DEFAULT_PROJECT_DIR = DEFAULT_BASE_DIR / "workspace"
DEFAULT_DATA_DIR = DEFAULT_BASE_DIR / "data"
DEFAULT_PORT = 18776


def _require_empty_root(state: dict[str, Any]) -> None:
    """Never turn an existing reconstruction or task into an empty root."""
    if not isinstance(state, dict):
        raise ValueError("数据目录里的工作台状态无效，请改用新的 --data-dir。")
    workspace = state.get("workspace", {})
    sessions = state.get("sessions", {})
    if (
        not isinstance(state.get("scene"), dict)
        or state["scene"].get("objects") != []
        or not isinstance(workspace, dict)
        or not isinstance(sessions, dict)
        or any(
            not isinstance(session, dict)
            or session.get("reference_images")
            or session.get("reference_clip")
            for session in sessions.values()
        )
        or state.get("feedback")
        or any(workspace.get(key) for key in (
            "thread_id", "created_thread_ids", "created_thread_specs", "queue",
            "active_feedback_id", "approvals", "request_feedback",
        ))
    ):
        raise ValueError(
            "指定数据目录不是空白工作台，请改用新的 --data-dir；不会清空现有场景。"
        )


def create_blank_server(
    *,
    port: int = DEFAULT_PORT,
    project_dir: str | Path = DEFAULT_PROJECT_DIR,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    listen_host: str = "127.0.0.1",
    public_base_url: str | None = None,
    lan_access: str = "open",
):
    """Restore imported scenes, keeping the ordinary root URL empty."""
    project_path = Path(project_dir).expanduser().resolve()
    data_path = Path(data_dir).expanduser().resolve()
    state_path = data_path / "state.json"
    # Check before starting event delivery or changing any existing state. The
    # server also owns its normal exclusive data lock throughout its lifetime.
    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        _require_empty_root(saved)
        saved_workspace = saved.get("workspace", {})
        if saved_workspace.get("project_dir") not in {None, str(project_path)}:
            raise ValueError("数据目录属于其他工作目录，请改用新的 --data-dir。")
    if (data_path / "codex_app_server_thread.json").exists():
        raise ValueError("数据目录已有 Codex 任务，请改用新的 --data-dir。")
    project_path.mkdir(parents=True, exist_ok=True)
    server = make_server(
        port=port,
        project_dir=project_path,
        data_dir=data_path,
        web_dir=REPO_ROOT / "web",
        enable_codex=False,
        external_review=True,
        feedback_transport="mcp_events",
        listen_host=listen_host,
        public_base_url=public_base_url,
        lan_access=lan_access,
    )
    try:
        registry = server.project_registry
        root = registry.root
        root.gateway.blank_workbench = True
        with registry._lock, root.store.lock:
            _require_empty_root(root.store.state)
            # Name only an unnamed empty root. Saved names and imported project
            # data remain intact on restart; no scene/session is recreated.
            if not root.store.state["scene"].get("name"):
                root.name = WORKBENCH_NAME
                root.gateway.project_name = WORKBENCH_NAME
                registry._records[root.project_id]["name"] = WORKBENCH_NAME
                registry._save()
                root.store.state["scene"]["name"] = WORKBENCH_NAME
                root.store._save()
        return server
    except BaseException:
        server.server_close()
        raise


def workbench_url(server, *, lan_access: str = "open") -> str:
    """Use a bookmarkable root URL in the default direct-access mode."""
    session_id = server.workspace_gateway.state()["session_id"]
    full_url = server.browser_url(session_id)
    if lan_access == "link":
        return full_url
    parts = urlsplit(full_url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="打开空白工作台，再从左侧导入场景文件夹")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-dir", type=Path, default=DEFAULT_PROJECT_DIR)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--public-base-url", help="局域网地址，例如 http://192.168.3.157:18776")
    parser.add_argument("--lan-access", choices=("open", "link"), default="open")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        server = create_blank_server(**vars(args))
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"无法打开空白工作台：{exc}\n")
    print(f"空白工作台：{workbench_url(server, lan_access=args.lan_access)}", flush=True)

    def terminate_server(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
