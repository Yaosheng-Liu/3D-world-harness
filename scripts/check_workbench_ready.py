#!/usr/bin/env python3
"""Check ready scene folders without importing them or starting a model task."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from core import APIError
from ready_check import check_ready_folder


STATUS_LABELS = {
    "ready": "可导入",
    "warning": "可导入，有提示",
    "partial": "部分可导入",
    "blocked": "无法导入",
    "empty": "未发现已准备实例",
}


def _summary(report: dict[str, Any]) -> str:
    counters = report["counters"]
    lines = [
        f"检查目录：{report['path']}",
        f"结果：{STATUS_LABELS.get(report['status'], report['status'])}",
        "实例：通过 {ready} · 提示 {warning} · 阻止 {blocked} · 跳过 {skipped}".format(**counters),
    ]
    for instance in report["instances"]:
        lines.append(f"\n{STATUS_LABELS.get(instance['status'], instance['status'])} · {instance['name']}")
        lines.append(f"  {instance['path']}")
        metrics = instance.get("metrics", {})
        if metrics.get("view_count"):
            lines.append("  {view_count} 个机位，{frame_count} 帧，{camera_count} 帧带相机".format(**metrics))
        for check in instance.get("checks", []):
            if check["status"] in {"warning", "error"}:
                lines.append(f"  {'提示' if check['status'] == 'warning' else '阻止'}：{check['message']}")
    for skipped in report.get("skipped", []):
        reason = skipped.get("reason", skipped.get("error", "未作为独立实例检查"))
        lines.append(f"\n跳过 · {skipped.get('path', '')}：{reason}")
    if report.get("truncated"):
        lines.append("\n扫描达到边界，尚未检查全部目录；请选择更具体的子目录重试。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查工作台 ready 实例，不导入、不创建模型任务")
    parser.add_argument("folder", help="服务主机上的绝对目录路径")
    parser.add_argument("--json", action="store_true", help="向标准输出写入完整 JSON 报告")
    parser.add_argument("--output", type=Path, help="把 JSON 报告保存到指定的新文件；已有文件不覆盖")
    args = parser.parse_args(argv)
    try:
        report = check_ready_folder(args.folder)
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output is not None:
            # An explicit report is the only file this command writes. Never
            # overwrite an existing manifest, model, reference or old report.
            with args.output.expanduser().open("x", encoding="utf-8") as handle:
                handle.write(serialized)
    except (APIError, OSError, ValueError) as exc:
        message = exc.message if isinstance(exc, APIError) else str(exc)
        if args.json:
            print(json.dumps({"path": args.folder, "status": "invalid", "can_import": False,
                              "error": message}, ensure_ascii=False))
        else:
            print("无法检查 ready：" + message, file=sys.stderr)
        return 2
    print(serialized, end="") if args.json else print(_summary(report))
    if args.output is not None:
        print(f"JSON 报告已保存：{args.output.expanduser()}", file=sys.stderr)
    return 0 if report["status"] in {"ready", "warning"} and not report["truncated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
