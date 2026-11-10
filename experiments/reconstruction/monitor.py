"""Supervise experiment workers without retrying or changing scientific trials."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from common import matrix, read, write


TERMINAL = {"modeled", "failed"}


def snapshot(root):
    protocol = read(root / "protocol.json")
    trials, errors, pending, evaluated = [], [], [], 0
    for scene in protocol["scenes"]:
        for spec in matrix(protocol):
            identifier = f"{scene}__{spec['condition']}__{spec['model']}__{spec['effort']}"
            trial = root / "runs" / identifier
            state = read(trial / "status.json") if (trial / "status.json").exists() else {
                "stage": "interrupted" if trial.exists() else "queued"}
            trials.append({"id": identifier, "scene": scene, **spec, **state})
            if state["stage"] in {"failed", "interrupted"}:
                errors.append({"trial": identifier, "kind": "trial", "error": state.get("error", "Missing status")})
            if state["stage"] == "modeled":
                missing = [f"M{i}" for i in range(protocol["modeling_passes"])
                           if not (trial / "checkpoints" / f"M{i}" / "frozen.json").exists()]
                if missing:
                    errors.append({"trial": identifier, "kind": "missing_checkpoints", "checkpoints": missing})
            for frozen in sorted((trial / "checkpoints").glob("M*/frozen.json")):
                result = root / "evaluation_results" / identifier / frozen.parent.name
                if (result / "complete.json").exists():
                    evaluated += 1
                elif (result / "error.json").exists():
                    errors.append({"kind": "evaluation", **read(result / "error.json")})
                else:
                    pending.append({"trial": identifier, "checkpoint": frozen.parent.name})
    terminal = sum(t["stage"] in TERMINAL for t in trials)
    automated_finished = terminal == len(trials) and not pending
    return {"updated_at": time.time(), "trials": trials, "terminal_trials": terminal,
            "expected_trials": len(trials), "evaluated_checkpoints": evaluated,
            "pending_evaluations": pending, "issues": errors,
            "automated_finished": automated_finished,
            "outcome": ("finished_with_errors" if errors else "automatic_stages_complete")
                       if automated_finished else "running",
            "semantic_edit_verification": "pending_independent_visual_check"}


def worker_alive(record, script, root):
    if not record.exists():
        return False
    pid = read(record)["pid"]
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    except FileNotFoundError:
        return False
    expected = read(record).get("args")
    return (any(Path(arg).name == script for arg in arguments) and str(root) in arguments
            and (not expected or arguments[:-1] == expected))


def start_worker(root, key, script, arguments):
    argv = [sys.executable, str(Path(__file__).with_name(script)), "--root", str(root), *arguments]
    with (root / f"monitor-{key}.log").open("a") as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    write(root / f"{key}.json", {"pid": process.pid, "started_at": time.time(), "args": argv})
    return {"worker": key, "action": "started", "pid": process.pid, "at": time.time()}


def fill_worker_pool(root, runtime, protocol, state, settings, gate_pending, held):
    """Assign one queued trial per slot, reserving it before assigning another."""
    slots = [(model, model+"-coordinator" if slot == 0 else f"{model}-worker{slot+1}")
             for model in protocol["models"] for slot in range(settings["workers_per_model"])]
    reserved, free, actions = set(), [], []
    for model, key in slots:
        record = root/f"{key}.json"
        if worker_alive(record, "run.py", root):
            arguments = read(record)["args"]
            filters = {name: arguments[arguments.index("--"+name)+1] for name in ("scene", "condition", "effort")}
            reserved.add(f"{filters['scene']}__{filters['condition']}__{model}__{filters['effort']}")
        else:
            free.append((model, key))
    active = {t["id"] for t in state["trials"] if t["stage"] not in TERMINAL | {"queued"}}
    orphans = active-reserved
    for identifier in orphans:
        state["issues"].append({"kind":"orphaned_controller", "trial":identifier})
    blocked_models = {t["model"] for t in state["trials"] if t["id"] in orphans}
    reserved |= active
    allowed = set(settings["before_gate_trials"]) if gate_pending else None
    allowed_conditions = settings.get("allowed_conditions")
    disk_ready = shutil.disk_usage(root).free >= settings.get("min_free_disk_bytes", 0)
    if not disk_ready:
        state["issues"].append({"kind": "disk_capacity", "minimum_free_bytes": settings["min_free_disk_bytes"]})
    if not held and disk_ready:
        for model, key in free:
            if model in blocked_models:
                continue
            queued = next((t for t in state["trials"] if t["model"] == model and t["stage"] == "queued"
                           and (allowed_conditions is None or t["condition"] in allowed_conditions)
                           and sum(q["model"] == model and q["condition"] == t["condition"]
                                   and q["id"] in reserved for q in state["trials"])
                               < settings.get("condition_limits", {}).get(t["condition"], len(slots))
                           and t["id"] not in reserved and (allowed is None or t["id"] in allowed)), None)
            if queued is None:
                continue
            arguments = ["--runtime", str(runtime), "--model", model, "--scene", queued["scene"],
                         "--condition", queued["condition"], "--effort", queued["effort"]]
            actions.append(start_worker(root, key, "run.py", arguments))
            reserved.add(queued["id"])
    state["parallelism"] = {"target_trials":len(slots), "assigned_trials":sorted(reserved),
                            "gate_pending":gate_pending}
    return actions


def requeue_legacy_review_timeouts(root, protocol, trials):
    """Apply the no-deadline policy once to reviews started by older workers."""
    if "review_timeout_seconds" not in protocol or protocol["review_timeout_seconds"] is not None:
        return []
    recovered = []
    for trial in trials:
        if trial["stage"] != "failed" or "review_R" not in (trial.get("error") or ""):
            continue
        run = root/"runs"/trial["id"]
        calls = sorted(run.glob("calls/review_R*/usage.json"), key=lambda p: read(p).get("started_at", 0))
        if not calls or not (read(calls[-1]).get("error") or "").startswith("timeout after "):
            continue
        marker = run/"resume_history"/f"timeout_{calls[-1].parent.name}.json"
        if marker.exists():
            continue
        state = read(run/"status.json")
        write(marker, state)
        state.update(stage="queued", error=None, updated_at=time.time(), recovery="Resume same frozen review without a deadline")
        write(run/"status.json", state)
        recovered.append({"at":time.time(), "action":"review_requeued", "trial":trial["id"],
                          "source_call":str(calls[-1]), "reason":"Previous worker used the superseded review deadline"})
    if recovered:
        path = root/"workflow_recoveries.json"
        write(path, (read(path) if path.exists() else []) + recovered)
    return recovered


def tick(root, runtime):
    state = snapshot(root)
    protocol = read(root / "protocol.json")
    actions = requeue_legacy_review_timeouts(root, protocol, state["trials"])
    if actions:
        state = snapshot(root)
    finished = sorted((t for t in state["trials"] if t["stage"] in TERMINAL),
                      key=lambda t: t.get("updated_at", 0))
    limit = protocol.get("halt_after_consecutive_failures")
    hold = root / "launch_hold.json"
    if limit and len(finished) >= limit and all(t["stage"] == "failed" for t in finished[-limit:]):
        recent = finished[-limit:]
        history = root / "workflow_recoveries.json"
        diagnosed = any(
            record.get("action") == "resume_queue_after_validation_diagnosis"
            and set(record["previous_hold"]["trials"]) == {t["id"] for t in recent}
            and record["at"] >= max(t.get("updated_at", 0) for t in recent)
            for record in (read(history) if history.exists() else [])
        )
        if not hold.exists() and not diagnosed:
            write(hold, {"at": time.time(), "reason": "Consecutive trial failures require diagnosis",
                         "trials": [t["id"] for t in finished[-limit:]]})
    gate_path = root / "execution_gate.json"
    gate = read(gate_path) if gate_path.exists() else None
    gated_trial = None
    if gate and not gate.get("passed_at"):
        gated_trial = next(t for t in state["trials"] if t["id"] == gate["trial"])
        if gated_trial["stage"] == "failed":
            write(hold, {"at": time.time(), "reason": "Full-chain verification trial failed", "trial": gate["trial"]})
        if any((root / "evaluation_results" / gate["trial"] / f"M{i}" / "error.json").exists()
               for i in range(protocol["modeling_passes"])):
            write(hold, {"at": time.time(), "reason": "Full-chain verification evaluation failed", "trial": gate["trial"]})
        evaluated = all((root / "evaluation_results" / gate["trial"] / f"M{i}" / "complete.json").exists()
                        for i in range(protocol["modeling_passes"]))
        if gated_trial["stage"] == "modeled" and evaluated:
            gate["passed_at"] = time.time()
            write(gate_path, gate)
            gated_trial = None
    # A crashed controller may leave a live model call. Keep that trial intact for
    # diagnosis rather than skipping it or silently starting a replacement.
    if not state["automated_finished"]:
        scheduler = root/"scheduler.json"
        if scheduler.exists():
            actions += fill_worker_pool(root, runtime, protocol, state, read(scheduler), bool(gated_trial), hold.exists())
        for model in ([] if scheduler.exists() else protocol["models"]):
            key = model + "-coordinator"
            if hold.exists() or (gated_trial and gated_trial["model"] != model):
                continue
            if worker_alive(root / f"{key}.json", "run.py", root):
                continue
            trials = [t for t in state["trials"] if t["model"] == model]
            orphans = [t["id"] for t in trials if t["stage"] not in TERMINAL | {"queued"}]
            if orphans:
                state["issues"].append({"kind": "orphaned_controller", "model": model, "trials": orphans})
            elif any(t["stage"] == "queued" for t in trials):
                arguments = ["--runtime", str(runtime), "--model", model]
                if gated_trial:
                    arguments += ["--scene", gated_trial["scene"], "--condition", gated_trial["condition"],
                                  "--effort", gated_trial["effort"]]
                actions.append(start_worker(root, key, "run.py", arguments))
        if not worker_alive(root / "report-process.json", "report.py", root):
            actions.append(start_worker(root, "report-process", "report.py", ["--watch"]))
        if not worker_alive(root / "evaluation-process.json", "evaluate.py", root):
            actions.append(start_worker(root, "evaluation-process", "evaluate.py", ["--runtime", str(runtime), "--watch"]))
    state["actions"] = actions
    state["execution_gate"] = gate
    state["launch_hold"] = read(hold) if hold.exists() else None
    if state["launch_hold"]:
        state["outcome"] = "needs_attention"
    state["free_disk_bytes"] = shutil.disk_usage(root).free
    write(root / "monitor.json", state)
    for action in actions:
        with (root / "monitor_actions.jsonl").open("a") as stream:
            stream.write(json.dumps(action) + "\n")
    checked_at = datetime.fromtimestamp(state["updated_at"], timezone(timedelta(hours=8)))
    lines = ["# 实验监控", "", f"检查时间：{checked_at:%Y-%m-%d %H:%M:%S} UTC+08:00",
             f"阶段：{state['outcome']}；已结束 {state['terminal_trials']}/{state['expected_trials']} 次试验；"
             f"已评测 {state['evaluated_checkpoints']} 个版本，待评测 {len(state['pending_evaluations'])} 个。", "",
             f"记录问题 {len(state['issues'])} 项；剩余磁盘 {state['free_disk_bytes']/2**30:.1f} GiB。", "",
             "完整进度及Token见 STATUS.md / progress.json；恢复记录见 monitor_actions.jsonl。",
             "失败模型调用和评测错误保留；未完成检查点不计成功。语义编辑归属仍单独核验。"]
    if state["issues"]:
        lines += ["", "待处理记录：", *[f"- {issue}" for issue in state["issues"]]]
    if state["launch_hold"]:
        lines += ["", "后续启动已停止，待处理：" + str(state["launch_hold"])]
    elif gated_trial:
        lines += ["", "完整验证通过前仅运行登记的验证组，随后放行其余队列：" + gated_trial["id"]]
    if state.get("parallelism"):
        pool = state["parallelism"]
        lines += ["", f"并行上限 {pool['target_trials']} 组，已分配 {len(pool['assigned_trials'])} 组；每个槽位独立运行一个实验。"]
    (root / "MONITOR.md").write_text("\n".join(lines) + "\n")
    if state["automated_finished"]:
        write(root / "automatic_completion.json", {k: v for k, v in state.items() if k != "trials"})
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.root = args.root.resolve()
    with (args.root / "monitor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            state = tick(args.root, args.runtime)
            if args.once or state["automated_finished"]:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
