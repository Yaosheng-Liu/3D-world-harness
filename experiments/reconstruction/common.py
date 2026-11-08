"""Small shared helpers for immutable experiment records and CLI usage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time


REPO = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def command(arguments, log, *, timeout=3600, env=None):
    started = time.time()
    with Path(log).open("w") as stream:
        result = subprocess.run(arguments, stdout=stream, stderr=subprocess.STDOUT,
                                env=env, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); see {log}")
    return time.time() - started


def cli_usage(path):
    """Read the last reported snapshot; exec resume reports cumulative usage."""
    thread_id, completed, errors = None, [], []
    for line in Path(path).read_text().splitlines():
        event = json.loads(line)
        if event.get("type") == "thread.started":
            thread_id = event["thread_id"]
        if event.get("type") == "turn.completed":
            completed.append(event.get("usage"))
        if event.get("type") in {"error", "turn.failed"}:
            errors.append(event.get("message") or event.get("error"))
    keys = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens")
    final = completed[-1] if completed else None
    totals = {key: final.get(key) if final else None for key in keys}
    totals["total_tokens"] = (totals["input_tokens"] + totals["output_tokens"]
                              if totals["input_tokens"] is not None and totals["output_tokens"] is not None else None)
    return {"thread_id": thread_id, "completed_turns": len(completed), "usage": totals,
            "errors": errors, "events_sha256": digest(path)}


def matrix(protocol):
    rows = []
    for model in protocol["models"]:
        for condition in protocol["conditions"]:
            rows.append({"model": model, "condition": condition, "effort": protocol["main_effort"]})
        rows.append({"model": model, "condition": "harness_tools", "effort": protocol["ablation_effort"]})
    return rows


def docker(runtime, mounts, script, arguments, *, gpu=False):
    result = ["docker", "run", "--rm", "--network", "none", "--user", "1000:1000",
              "-e", "OMP_NUM_THREADS=4", "-e", "OPENBLAS_NUM_THREADS=4"]
    if gpu:
        result += ["--gpus", "all", "--ipc", "host"]
    for path, mode in mounts:
        result += ["-v", f"{path}:{path}:{mode}"]
    result += ["-v", f"{runtime['da3_code']}:/opt/da3:ro", "-e", "PYTHONPATH=/opt/da3/src",
               "--entrypoint", "python", runtime["geometry_image"], str(script), *map(str, arguments)]
    return result
