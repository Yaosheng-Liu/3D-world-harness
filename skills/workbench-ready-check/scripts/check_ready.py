#!/usr/bin/env python3
"""Locate the workbench runtime and delegate to its shared ready checker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def runtime_root(explicit: str | None) -> Path | None:
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        configured = os.environ.get("SCENE_FEEDBACK_ROOT")
        candidates = ([Path(configured).expanduser()] if configured else [])
        candidates.extend(Path(__file__).resolve().parents)
        candidates.extend([Path.cwd(), *Path.cwd().parents])
        candidates.append(Path.home() / "scene_feedback_harness")
    for candidate in candidates:
        if ((candidate / "scripts/check_workbench_ready.py").is_file()
                and (candidate / "backend/ready_check.py").is_file()):
            return candidate.resolve()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--harness-root")
    args, remaining = parser.parse_known_args()
    root = runtime_root(args.harness_root)
    if root is None:
        print("Workbench checker runtime not found. Pass --harness-root /absolute/path/to/checkout.",
              file=sys.stderr)
        return 2
    python = root / ".venv/bin/python"
    command = [str(python) if python.is_file() else sys.executable,
               str(root / "scripts/check_workbench_ready.py"), *remaining]
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        print(f"Cannot run workbench checker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
