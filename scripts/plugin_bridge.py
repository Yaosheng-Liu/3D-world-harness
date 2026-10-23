#!/usr/bin/env python3
"""Portable stdio bridge to one explicitly configured workbench MCP endpoint.

No server discovery, default workspace, Desktop database access or task wakeup.
Uses only the Python standard library; runtime dependencies belong to the server.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_RESPONSE = 64 * 1024 * 1024
MAX_REQUEST = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class Bridge:
    def __init__(self, env: dict | None = None):
        env = os.environ if env is None else env
        names = ("SCENE_FEEDBACK_PROJECT_ID", "SCENE_FEEDBACK_DATA_DIR", "SCENE_FEEDBACK_PROJECT_DIR", "SCENE_FEEDBACK_PORT")
        if any(not env.get(name) for name in names):
            raise ValueError("Configure the plugin with scripts/configure_plugin.py and an explicit project ID, data directory, project directory and port")
        self.project_id = env[names[0]]
        if not re.fullmatch(r"[0-9a-f]{32}", self.project_id):
            raise ValueError("Invalid configured project ID")
        self.data_dir = Path(env[names[1]]).expanduser().resolve(strict=True)
        self.project_dir = Path(env[names[2]]).expanduser().resolve(strict=True)
        port = int(env[names[3]])
        if not 1 <= port <= 65535 or not self.data_dir.is_dir() or not self.project_dir.is_dir():
            raise ValueError("Invalid explicit workspace configuration")
        self.url = env.get("SCENE_FEEDBACK_MCP_URL") or f"http://127.0.0.1:{port}/p/{self.project_id}/mcp"
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != f"/p/{self.project_id}/mcp"
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("Endpoint must match this project and use HTTPS or loopback HTTP")
        self.health_url = self.url.rsplit("/", 1)[0] + "/api/health"
        token = (self.data_dir / "control_token").read_text(encoding="utf-8").strip()
        if not token or any(char.isspace() for char in token):
            raise ValueError("Invalid workspace credential file")
        self.headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
        self.health_headers = {**self.headers, "X-Scene-Harness-Key": token}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(self, request: urllib.request.Request) -> dict | None:
        with self.opener.open(request, timeout=360) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("MCP response exceeded bridge size limit")
            if response.status == 202 and not raw:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("MCP response must be an object")
            return value

    def check_workspace(self) -> None:
        health = self.read(urllib.request.Request(self.health_url, headers=self.health_headers))
        if (not health or health.get("service") != "scene-feedback-harness"
                or health.get("project_id") != self.project_id
                or health.get("data_dir") != str(self.data_dir)
                or health.get("project_dir") != str(self.project_dir)
                or health.get("feedback_transport") != "mcp_events"):
            raise ValueError("Endpoint differs from the configured event workspace; check project ID, directories and --mcp-events")

    def forward(self, request: dict) -> dict | None:
        self.check_workspace()
        body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST:
            raise ValueError("MCP request exceeds 1 MiB")
        return self.read(urllib.request.Request(self.url, data=body, headers={**self.headers, "Content-Type": "application/json"}))


def main() -> int:
    try:
        bridge = Bridge()
    except (ValueError, OSError):
        print("Scene Feedback plugin requires a configured, running --mcp-events workspace. See docs/mcp-events-plugin.md.", file=sys.stderr)
        return 1
    for line in sys.stdin.buffer:
        request_id = None
        notification = False
        try:
            if len(line) > MAX_REQUEST:
                raise ValueError("MCP request exceeds 1 MiB")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request must be an object")
            request_id = request.get("id")
            notification = "id" not in request
            result = bridge.forward(request)
        except (ValueError, OSError, urllib.error.URLError):
            # Neither response bodies nor configured URLs/credentials enter stderr.
            result = {"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32000, "message": "Configured workbench unavailable or request invalid; check the event server and plugin configuration"}}
        if result is not None and not notification:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
