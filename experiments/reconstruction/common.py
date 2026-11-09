"""Small shared helpers for immutable experiment records and CLI usage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
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


def camera_manifest(data, training_frames):
    """Map supplied poses to the prepared pinhole images in every trial packet."""
    intrinsics = {key: data[source] for key, source in
                  {"width":"w", "height":"h", "fx":"fl_x", "fy":"fl_y", "cx":"cx", "cy":"cy"}.items()}
    frames = []
    for index, frame in enumerate(data["frames"]):
        role = "train" if index < training_frames else "heldout"
        frames.append({"index": index, "role": role, "source_index": frame["source_index"],
                       "image": f"observations/{role}/{index:06d}.png",
                       "camera_to_world": frame["transform_matrix"]})
    return {"camera_convention":"OpenGL camera-to-world; image +x right, camera -z forward",
            "image_calibration":"Prepared pinhole images; see calibration_audit.json for distortion handling",
            "intrinsics": intrinsics, "frames": frames}


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


def thread_usage(home, thread_id):
    """Read the last reported cumulative snapshot, including interrupted turns."""
    snapshot = {}
    for path in sorted((Path(home) / "sessions").rglob(f"*{thread_id}.jsonl")):
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A killed streaming writer can leave its final line incomplete.
                payload = event.get("payload", {})
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    snapshot = info.get("total_token_usage") or snapshot
    return snapshot


def model_handoff(source):
    processes = sorted(source.glob("calls/model_M*/process.json"), key=lambda p: read(p)["started_at"])
    if not processes:
        return {"thread_id": None, "raw_cumulative_usage": None}
    call = processes[-1].parent
    usage = read(call/"usage.json") if (call/"usage.json").exists() else cli_usage(call/"events.jsonl")
    thread = usage.get("thread_id") or read(call/"process.json").get("thread_id")
    raw = dict(usage.get("raw_cumulative_usage", usage["usage"]))
    snapshot = thread_usage(source/"homes/modeler", thread) if thread else {}
    for key in raw:
        if raw[key] is None:
            raw[key] = snapshot.get(key)
    return {"thread_id": thread, "raw_cumulative_usage": raw}


def bind_candidate_sources(project, candidate):
    """The controller owns immutable input lineage; the agent owns scene content."""
    scene = read(project / candidate)
    spec = read(project / "experiment.json")
    if spec.get("appearance_regime"):
        from reconstruction_appearance import validate_appearance
        validate_appearance(scene, spec["appearance_regime"])
        # Both new regimes use the same display transform regardless of shader type.
        scene["color_management"] = {"view_transform": "Standard", "exposure": 0}
    names = ["observations/manifest.json", "cameras.json"]
    if spec["condition"] != "single_basic":
        names += ["dense/inference.json", "reconstruction_report.json"]
    sources = scene["provenance"]["sources"]
    existing = {source["path"] for source in sources}
    for name in names:
        if name not in existing:
            sources.append({"path": name, "description": "Fixed experiment input lineage, bound by controller"})
    write(project / candidate, scene)
    return scene


def checkpoint_calls(trial, number):
    records = []
    if (trial / "planner_usage.json").exists():
        records.append({"phase_role": "planner", **read(trial / "planner_usage.json")})
    for directory in ("source_calls", "calls"):
        for path in sorted((trial / directory).glob("*/usage.json")):
            match = re.fullmatch(r"(model_M|review_R)(\d+)(?:_attempt\d+)?", path.parent.name)
            if match and int(match[2]) <= number:
                records.append({"phase_role": "modeler" if match[1] == "model_M" else "reviewer", **read(path)})
    return records


def usage_summary(records):
    """Latest cumulative counter per model thread, plus independent role calls."""
    counters, latest = [], {}
    for index, record in enumerate(records):
        if record["phase_role"] == "modeler":
            key = record.get("thread_id") or f"untracked_{index}"
            if key not in latest or record.get("started_at", 0) >= latest[key].get("started_at", 0):
                latest[key] = record
        else:
            counters.append(record["usage"])
    counters += [r.get("raw_cumulative_usage", r["usage"]) for r in latest.values()]
    return {key: sum(c[key] for c in counters) if counters and all(c.get(key) is not None for c in counters) else None
            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens", "total_tokens")}


def matrix(protocol):
    rows = []
    for model in protocol["models"]:
        for condition in protocol["conditions"]:
            rows.append({"model": model, "condition": condition, "effort": protocol["main_effort"]})
        if protocol.get("ablation_effort"):
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
