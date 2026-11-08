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
            trials.append({"id": identifier, "model": spec["model"], **state})
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


def tick(root, runtime):
    state = snapshot(root)
    actions = []
    # A crashed controller may leave a live model call. Keep that trial intact for
    # diagnosis rather than skipping it or silently starting a replacement.
    if not state["automated_finished"]:
        for model in read(root / "protocol.json")["models"]:
            key = model + "-coordinator"
            if worker_alive(root / f"{key}.json", "run.py", root):
                continue
            trials = [t for t in state["trials"] if t["model"] == model]
            orphans = [t["id"] for t in trials if t["stage"] not in TERMINAL | {"queued"}]
            if orphans:
                state["issues"].append({"kind": "orphaned_controller", "model": model, "trials": orphans})
            elif any(t["stage"] == "queued" for t in trials):
                actions.append(start_worker(root, key, "run.py", ["--runtime", str(runtime), "--model", model]))
        if not worker_alive(root / "report-process.json", "report.py", root):
            actions.append(start_worker(root, "report-process", "report.py", ["--watch"]))
        if not worker_alive(root / "evaluation-process.json", "evaluate.py", root):
            actions.append(start_worker(root, "evaluation-process", "evaluate.py", ["--runtime", str(runtime), "--watch"]))
    state["actions"] = actions
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
