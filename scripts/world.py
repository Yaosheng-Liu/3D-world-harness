#!/usr/bin/env python3
"""Run the same world operations as the Agent tools, without starting a server."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from core import APIError
from world_model import WorldError, WorldStore
from world_service import WorldService


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="Editable project directory on this host")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize from a world JSON manifest").add_argument("source", type=Path)
    commands.add_parser("inspect").add_argument("--object-id")
    for command in ("edit", "restore", "build", "simulate", "evaluate"):
        sub = commands.add_parser(command)
        sub.add_argument("--expected-revision", required=True, type=int)
        if command == "edit":
            sub.add_argument("--changes", required=True, type=Path, help="JSON array of add/update/delete operations")
        elif command == "restore":
            sub.add_argument("--revision", required=True, type=int)
        elif command == "build":
            sub.add_argument("--render", action="store_true")
            sub.add_argument("--simulation-id")
        elif command == "simulate":
            sub.add_argument("--action", type=Path, help="JSON force, impulse or joint_force action")
            sub.add_argument("--duration-sec", type=float, default=2)
            sub.add_argument("--sample-hz", type=float, default=30)
        elif command == "evaluate":
            sub.add_argument("--build-id", required=True)
            sub.add_argument("--simulation-id")
    options = vars(parser.parse_args())
    project, command = options.pop("project"), options.pop("command")
    try:
        if command == "init":
            result = WorldStore(project).initialize(read_json(options["source"]))
        else:
            options = {key: value for key, value in options.items() if value is not None}
            for key in ("changes", "action"):
                if key in options:
                    options[key] = read_json(options[key])
            result = WorldService(project).call("workspace_" + command + "_world", options)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (APIError, WorldError, OSError, ValueError) as exc:
        parser.exit(1, f"world: {exc}\n")


if __name__ == "__main__":
    main()
