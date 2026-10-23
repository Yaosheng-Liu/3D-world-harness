#!/usr/bin/env python3
"""Modeler -> frozen Blender checks -> read-only Codex critic -> repair or publish."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from reconstruction_review import (REVIEW_SCHEMA, freeze_candidate, project_file, read_json,
                                   validate_review, verify_files, write_json)
from world_model import WorldStore


class Workbench:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.opener = build_opener(ProxyHandler({}))
        self.capability = self.call("/api/workspace/state")["browser_capability"]

    def call(self, path, payload=None):
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            headers["X-Workspace-Capability"] = self.capability
        request = Request(self.url + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with self.opener.open(request, timeout=330) as response:
            return json.load(response)

    def wait_builder(self, feedback_id):
        while True:
            state = self.call("/api/workspace/state")
            item = next(item for item in state["queue"] if item["feedback_id"] == feedback_id)
            if item["status"] == "completed":
                return state
            if item["status"] in {"failed", "cancelled", "delivery_uncertain", "blocked_stale"}:
                raise RuntimeError(f"builder {item['status']}: {item.get('error')}")
            # Approvals are shown by the existing workbench, never silently accepted here.
            time.sleep(5)

    def repair(self, note, key):
        state = self.call("/api/workspace/state")
        if state["model_settings"] != {"model": "gpt-6-astra", "reasoning_effort": "high"}:
            raise RuntimeError("builder must remain gpt-6-astra / high")
        if state["agent"]["status"] != "idle" or state["approvals"]:
            raise RuntimeError("finish the current workbench task before scheduling repairs")
        return self.call(f"/api/sessions/{state['session_id']}/feedback", {
            "scene_revision": state["scene_revision"], "note": note, "idempotency_key": key,
        })["feedback_id"]


def review_prompt(project, frozen, previous):
    manifest = read_json(frozen / "candidate.json")
    return f"""You are the independent visual reconstruction critic, not the modeler.
User goal: convincingly reconstruct this real scene, with complete individually editable objects. Physics is DEFERRED.
Read-only task: do not modify any files, run modeling scripts, publish, or call any write-capable service. Output only the review JSON matching the schema.
Project: {project}
Frozen candidate: {frozen / 'candidate.json'}
Candidate ID: {manifest['candidate_id']}
Read scene.json, build.json, editability.json, metrics.json and inspection.json in {frozen}. The attached comparison images show reference, PURE render, overlay, error, in that order. Judge the pure render, never the reference overlay. Actually inspect every attached image and additional source/crop views using image viewing tools. All six existing evaluation views have already been seen; they are regression views, not blind ground truth.
Check every object: silhouette, completeness, support/occlusion, texture seams, missing surfaces, neighboring objects accidentally merged, duplicated remnants after separation, meaningful pivots and parent hierarchy. Test report proves transform isolation only, not semantic segmentation. Unobserved backs may be explicitly estimated; visible large defects or a generic box substituted for detailed furniture fail. A phototextured triangle soup or camera-facing photo planes do not satisfy the goal. Full-frame PSNR is diagnostic, never sufficient for acceptance.
Appearance, structure and editability must ALL pass for accepted. Unknown or uncertain quality is revise/blocked. Review cannot waive deterministic check failures. Be strict but evidence-based; don't invent unseen real geometry. Scope is the real visible furniture showroom, not only its main bed. The main bed must contain separately editable frame/headboard/mattress/pillows/cover and nearby tables/walls/floor must not move with it.
Issue IDs are stable across rounds. Every issue gives a real object_id and view_id from the candidate, normalized [x0,y0,x1,y1] region, specific visual evidence and a reproducible acceptance criterion. Keep strongest blocking issues actionable; do not use vague 'improve realism' comments. Include all actually reviewed fixed view IDs and checked object IDs. Previous review to recheck: {previous or 'none'}. Every prior issue must either remain with its ID or appear in resolved_issue_ids, with no silent dropping. Do not trust the modeler's self-assessment.\n"""


def run(options):
    project = options.project.resolve(strict=True)
    workbench = Workbench(options.url)
    world = WorldStore(project).read()
    current = workbench.call("/api/workspace/state")
    if Path(current["project_dir"]).resolve() != project:
        raise ValueError("workbench URL belongs to another project")
    if current["model_settings"] != {"model": "gpt-6-astra", "reasoning_effort": "high"}:
        raise ValueError("builder must be Astra High")
    directory = project / "reconstruction"
    directory.mkdir(exist_ok=True)
    with (directory / ".run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        run_dir = directory / run_id
        run_dir.mkdir()
        status = {"run_id": run_id, "builder": {"model": "gpt-6-astra", "effort": "high", "thread_id": current["thread_id"]},
                  "reviewer": {"model": options.review_model, "effort": "ultra", "requested_model": options.requested_review_model},
                  "base_world_revision": world["revision"], "base_scene_revision": current["scene_revision"],
                  "physics": "deferred", "accepted": False, "round": 1, "max_rounds": options.max_rounds,
                  "feedback_id": options.feedback_id, "issues": [], "pid": os.getpid()}

        def update(stage, **fields):
            status.update(stage=stage, updated_at=datetime.now(timezone.utc).isoformat(), **fields)
            write_json(directory / "status.json", status)
            write_json(run_dir / "status.json", status)
            print(json.dumps({"stage": stage, "round": status["round"], **fields}, ensure_ascii=False), flush=True)

        candidate_path, receipt_path = options.candidate, options.receipt
        feedback_id, previous = options.feedback_id, None
        try:
            for number in range(1, options.max_rounds + 1):
                update("modeling", round=number, feedback_id=feedback_id)
                if feedback_id:
                    workbench.wait_builder(feedback_id)
                update("checking")
                frozen = run_dir / f"round_{number:02d}"
                manifest = freeze_candidate(project, candidate_path, receipt_path, frozen, blender=options.blender,
                                            previous_review=previous)
                update("reviewing", candidate_id=manifest["candidate_id"], checks=manifest["checks"],
                       object_count=len(manifest["object_ids"]), review_directory=str(frozen.relative_to(project)),
                       views=read_json(frozen / "scene.json")["views"])
                schema = frozen / "review_schema.json"
                write_json(schema, REVIEW_SCHEMA)
                prompt = review_prompt(project, frozen, previous)
                (frozen / "review_prompt.txt").write_text(prompt)
                command = [options.codex, "exec", "-C", str(project), "--skip-git-repo-check", "-s", "read-only",
                           "-c", 'approval_policy="never"', "-m", options.review_model,
                           "-c", 'model_reasoning_effort="ultra"', "--json", "--output-schema", str(schema),
                           "--output-last-message", str(frozen / "review.json")]
                inspection = read_json(frozen / "inspection.json")
                for path in inspection.get("inspection_paths", []):
                    command.extend(["--image", str(project_file(project, path))])
                for name in ("edit_before.png", "edit_after.png"):
                    command.extend(["--image", str(frozen / name)])
                command.append("-")
                with (frozen / "review_events.jsonl").open("w") as events, (frozen / "review_stderr.log").open("w") as errors:
                    result = subprocess.run(command, input=prompt, text=True, stdout=events, stderr=errors, timeout=2400)
                # The critic has a fresh, separately persisted thread and no write approvals.
                thread = next((json.loads(line)["thread_id"] for line in (frozen / "review_events.jsonl").read_text().splitlines()
                               if json.loads(line).get("type") == "thread.started"), None)
                status["reviewer"]["thread_id"] = thread
                if result.returncode:
                    raise RuntimeError(f"critic failed ({result.returncode}); inspect {frozen / 'review_events.jsonl'}")
                verify_files(project, manifest["files"])
                review = read_json(frozen / "review.json")
                accepted = validate_review(manifest, review, read_json(previous) if previous else None)
                update("reviewed", summary=review["summary"], dimensions=review["dimensions"], issues=review["issues"])
                if accepted:
                    update("publishing")
                    result = workbench.call("/api/workspace/reconstruction/accept", {
                        "candidate_path": str((frozen / "candidate.json").relative_to(project)),
                        "review_path": str((frozen / "review.json").relative_to(project)),
                        "expected_revision": world["revision"], "expected_scene_revision": current["scene_revision"],
                    })
                    write_json(frozen / "publication.json", result)
                    update("accepted", accepted=True, world_revision=result["world_revision"], scene_revision=result["scene"]["revision"])
                    return
                if review["verdict"] == "blocked" or number == options.max_rounds:
                    update("needs_revision", reason="审核未通过；需处理证据或调整建模方法后开启下一轮。")
                    return
                previous = frozen / "review.json"
                next_dir = Path(candidate_path).parent / f"repair_{number + 1:02d}"
                candidate_path, receipt_path = str(next_dir / "candidate.json"), str(next_dir / "build_receipt.json")
                note = f"""独立审核未通过。继续同一真实场景建模任务，先实际查看 {previous} 和 {frozen / 'inspection.json'} 中图像，再处理阻塞问题。逐项修复并检查回归，不要把改名/多分几组当作真实外观完成。所有原候选与审核证据保持不变；新资产、脚本放 {next_dir}。输出 {candidate_path}（revision={world['revision'] + 1}）和 {receipt_path}，使用 build_world(scene=candidate, render=True)；保持原世界坐标和验证相机。不要修改 world/scene.json，不发布，不修改仓库或审核记录。父子对象与本地原点必须通过实际移动旋转测试。物理延后。优先修复这些问题：{json.dumps(review['issues'][:5], ensure_ascii=False)}。检查其余旧问题，不能遗漏。"""
                update("repairing")
                feedback_id = workbench.repair(note, run_id + f"-repair-{number + 1}")
        except Exception as exc:
            update("failed", error=str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--feedback-id", help="Wait for this already submitted builder task before freezing")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--review-model", default="gpt-6.1-sol")
    parser.add_argument("--requested-review-model", default="gpt-6.1-sol")
    parser.add_argument("--max-rounds", type=int, choices=range(1, 4), default=3)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
