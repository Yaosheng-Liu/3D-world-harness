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
from urllib.error import HTTPError
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from reconstruction_review import (freeze_candidate, project_file, read_json, review_schema,
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

    def refresh_preview(self, project, state):
        receipts = list((Path(project) / "world/builds").glob("*/receipt.json"))
        if not receipts:
            return
        receipt = max(receipts, key=lambda path: path.stat().st_mtime_ns)
        version = (str(receipt), receipt.stat().st_mtime_ns)
        if version == getattr(self, "_last_preview", None):
            return
        try:
            self.call("/api/workspace/reconstruction/preview", {
                "receipt_path": str(receipt.relative_to(project)), "expected_scene_revision": state["scene_revision"],
                "summary": "建模阶段更新；可选中、标注并提交修改意见，尚未验收"})
        except HTTPError as error:
            # Preview failure must not discard modeling work or bypass review.
            print(json.dumps({"preview_error": error.read().decode()[:500], "receipt": str(receipt)}), flush=True)
        self._last_preview = version

    def wait_builder(self, feedback_id, *, preview_project=None):
        while True:
            state = self.call("/api/workspace/state")
            item = next(item for item in state["queue"] if item["feedback_id"] == feedback_id)
            if preview_project is not None and item["status"] in {"running", "completed"}:
                self.refresh_preview(preview_project, state)
            if item["status"] == "completed":
                return state
            if item["status"] in {"failed", "cancelled", "interrupted", "delivery_uncertain", "blocked_stale"}:
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
    planned = ""
    if manifest.get("plan_path"):
        planned = f"""
This candidate belongs to the initialized object-modeling workflow. Read the frozen initial plan ({manifest['plan_path']}), segments ({manifest['segments_path']}), geometry ({manifest['geometry_path']}), candidate modeling report ({manifest['modeling_report_path']}) and inventory check ({manifest['inventory_path']}). Planning, SAM3, DA3 and TSDF are INITIALIZATION EVIDENCE ONLY, not final geometry or authoritative object granularity. The builder must reconstruct and refine each object in Blender from the original images, including missed objects, masked-out parts, holes and texture ownership. It may refine parts and hierarchy while retaining coverage of observed objects. Check every report mapping and exclusion against the referenced original frames: a one-to-many or many-to-one mapping must reflect actual corrected object structure, not hide missing geometry. Initial unexplained regions can be resolved by the builder's report and final model; they do not require a revised initial plan. Do not require the builder to preserve an incorrect initial segmentation or rerun initialization before fixing a model.
Independently inspect EVERY original training view in source_observations of candidate.json, including areas not mentioned by the planner. Attached source contact sheets cover all frames; open full-resolution originals for small objects, uncertain boundaries and possible omissions. Do not claim a source view was inspected merely because its ID is listed. Recheck exclusions and the builder's handling of initially unexplained regions against the original evidence. A SAM miss must never silently remove an object from acceptance.
Every issue has category: inventory, segmentation, spatial_structure, silhouette, texture_alignment, lighting, geometry, appearance or editability. These categories describe defects, NOT routing: ALL issues go directly to the same modeling agent for object-by-object reconstruction and repair. In a segmentation issue, specify the required corrected object boundary or texture ownership in the final Blender model, not a request to rerun SAM. A poor initialization mask or mesh is not itself a final-model defect if the builder has corrected it. Use a planned object ID even if it is missing from the candidate. Use object_id='scene' for a newly discovered object outside the plan, citing its exact source_<frame_index> view and region. Actual candidate IDs are also valid. Inspect wall/floor residues and duplicated object textures after hiding or moving an object. Check final hierarchy, completeness and editable geometry independently; an initialization fragment cannot stand in for a completed object. A high image metric cannot waive inventory failures.
Check spatial_structure FIRST for EVERY object, its parts and relationships to other objects, not only room architecture: reversed protrusions/recesses or inside/outside, incorrect relative depth/orientation/thickness, misplaced connections/openings, interpenetration and unsupported gaps. Examples include a cushion depression modeled as a bulge, a handle embedded in a cabinet, swapped overlapping fabric layers, or legs attached on the wrong side. State the relevant reference surface/object/part and local direction explicitly; do not require a wall or planar reference for curved objects. Distinguish within-object shape from between-object placement; identify related object IDs in evidence when present. Verify camera alignment, coordinate conventions and candidate surface orientation before diagnosing the shape. Compare at least two original views with translation parallax and visible boundaries when available, using occlusion, visible side faces and reliable depth as supporting evidence. Bright/dark regions alone cannot determine concavity; DA3/TSDF predictions are not independent ground truth. Request neutral-material, depth/normal or plan/section diagnostics if needed; these reveal candidate geometry, not the true scene. Do not claim nonexistent diagnostics were inspected. If observations cannot resolve the spatial relationship, state that uncertainty and the missing evidence rather than assert a shape or placement. Acceptance must specify the expected relative position, orientation, thickness, contact or containment as applicable, and consistent occlusion/side faces across cited views, without invented metric dimensions or unseen interior geometry. Unresolved object or scene structure cannot be passed because textures or lighting hide it.
Use silhouette for mismatched contours, proportions or drape; texture_alignment for displaced/stretched patterns, UV seams or projection ghosting; lighting for misplaced light footprints, shadow direction/softness, illumination color or exposure. Use geometry for other structural defects and appearance for intrinsic material/color/roughness defects or an appearance mismatch whose cause is not yet established. Split independently verifiable defects into separate issues even on the same object. In evidence, separate observed mismatch from inferred cause; do not label every color difference lighting. If uncertain, state the competing causes and the diagnostic comparison required. In acceptance_criterion, specify the repair target (geometry, UV/source projection, material or lights), matched reference camera(s) and observable passing condition. Diagnose with the frozen Cycles comparisons, not workbench navigation lighting; check camera alignment before attributing contour errors to geometry. Request cross-view verification where observed, and do not prescribe baking shadows or highlights into base color to hide lighting errors.
The object inspection contact sheets show BEFORE and HIDDEN renders for every actual candidate object, with its ID and selected view. Inspect every pair for leftover texture/geometry, wrongly disappearing neighbors and exposed holes; open the individual full images listed in editability.json when needed. Camera selection uses projected bounds, not guaranteed visibility. If occlusion prevents a meaningful check, request a better source-bound inspection view; a rendered pair alone does not prove correct separation.
Acceptance requires all fixed render view IDs AND all source_view_ids in reviewed_views, and all actual candidate object IDs in checked_objects, including newly modeled objects beyond the initial plan. Recheck all initial objects and unexplained regions through the bound modeling report. Do not list unchecked IDs simply to satisfy this gate.
"""
    return f"""You are the independent visual reconstruction critic, not the modeler.
User goal: convincingly reconstruct this real scene, with complete individually editable objects. Physics is DEFERRED.
Read-only task: do not modify any files, run modeling scripts, publish, or call any write-capable service. Output only the review JSON matching the schema.
Project: {project}
Frozen candidate: {frozen / 'candidate.json'}
Candidate ID: {manifest['candidate_id']}
Read scene.json, build.json, editability.json, metrics.json and inspection.json in {frozen}. The attached comparison images show reference, PURE render, overlay, error, in that order. Judge the pure render, never the reference overlay. Actually inspect every attached image and additional source/crop views using image viewing tools. Fixed evaluation views may already have been seen during modeling; do not describe them as blind ground truth.
Check every object: silhouette, completeness, support/occlusion, texture seams, missing surfaces, neighboring objects accidentally merged, duplicated remnants after separation, meaningful pivots and parent hierarchy. Test report proves transform isolation only, not semantic segmentation. Unobserved backs may be explicitly estimated; visible large defects or a generic box substituted for a detailed source object fail. A phototextured triangle soup or camera-facing photo planes do not satisfy the goal. Full-frame PSNR is diagnostic, never sufficient for acceptance.
Appearance, structure and editability must ALL pass for accepted. Unknown or uncertain quality is revise/blocked. Review cannot waive deterministic check failures. Be strict but evidence-based; don't invent unseen real geometry. Scope is the complete visible input scene. Identify each independently editable object and its meaningful parts from the original references; adjacent objects and scene structure must not move with it.
{planned}
Issue IDs are stable across rounds. Every issue gives an object_id and view_id bound to this candidate's manifest, normalized [x0,y0,x1,y1] region, specific visual evidence and a reproducible acceptance criterion. Keep strongest blocking issues actionable; do not use vague 'improve realism' comments. Include all actually reviewed fixed view IDs and checked object IDs. Previous review to recheck: {previous or 'none'}. Every prior issue must either remain with its ID or appear in resolved_issue_ids, with no silent dropping. Do not trust the modeler's self-assessment.\n"""


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
        modeling_report_path = getattr(options, "modeling_report", None)
        feedback_id, previous = options.feedback_id, getattr(options, "previous_review", None)
        try:
            for number in range(1, options.max_rounds + 1):
                update("modeling", round=number, feedback_id=feedback_id)
                if feedback_id:
                    workbench.wait_builder(feedback_id)
                update("checking")
                frozen = run_dir / f"round_{number:02d}"
                manifest = freeze_candidate(project, candidate_path, receipt_path, frozen, blender=options.blender,
                                            previous_review=previous, plan_path=getattr(options, "plan", None),
                                            segments_path=getattr(options, "segments", None), geometry_path=getattr(options, "geometry", None),
                                            modeling_report_path=modeling_report_path)
                update("reviewing", candidate_id=manifest["candidate_id"], checks=manifest["checks"],
                       object_count=len(manifest["object_ids"]), review_directory=str(frozen.relative_to(project)),
                       views=read_json(frozen / "scene.json")["views"] + [
                           {"id": row["view_id"], "reference_only": True} for row in manifest.get("source_observations", [])])
                schema = frozen / "review_schema.json"
                write_json(schema, review_schema(manifest))
                frozen_previous = project_file(project, manifest["previous_review_path"]) if manifest["previous_review_path"] else None
                prompt = review_prompt(project, frozen, frozen_previous)
                (frozen / "review_prompt.txt").write_text(prompt)
                command = [options.codex, "exec", "-C", str(project), "--skip-git-repo-check", "-s", "read-only",
                           "-c", 'approval_policy="never"', "-m", options.review_model,
                           "-c", 'model_reasoning_effort="ultra"', "--json", "--output-schema", str(schema),
                           "--output-last-message", str(frozen / "review.json")]
                inspection = read_json(frozen / "inspection.json")
                edit_check = read_json(frozen / "editability.json")
                for path in (inspection.get("inspection_paths", []) + manifest.get("source_inspection_paths", [])
                             + edit_check.get("inspection_paths", [])):
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
                accepted = validate_review(manifest, review, read_json(frozen_previous) if frozen_previous else None)
                update("reviewed", summary=review["summary"], dimensions=review["dimensions"], issues=review["issues"],
                       review_path=str((frozen / "review.json").relative_to(project)))
                if accepted:
                    if not getattr(options, "publish_after_review", True):
                        update("reviewed", review_passed=True, reason="审核通过；本轮保留候选，反馈建模完成后等待用户查看。")
                        return status
                    update("publishing")
                    result = workbench.call("/api/workspace/reconstruction/accept", {
                        "candidate_path": str((frozen / "candidate.json").relative_to(project)),
                        "review_path": str((frozen / "review.json").relative_to(project)),
                        "expected_revision": world["revision"], "expected_scene_revision": current["scene_revision"],
                    })
                    write_json(frozen / "publication.json", result)
                    update("accepted", accepted=True, world_revision=result["world_revision"], scene_revision=result["scene"]["revision"])
                    return status
                if review["verdict"] == "blocked" or number == options.max_rounds:
                    update("needs_revision", reason="审核未通过；需处理证据或调整建模方法后开启下一轮。")
                    return status
                previous = frozen / "review.json"
                next_dir = Path(candidate_path).parent / f"repair_{number + 1:02d}"
                candidate_path, receipt_path = str(next_dir / "candidate.json"), str(next_dir / "build_receipt.json")
                report_instruction = ""
                if manifest.get("plan_path"):
                    from reconstruction_plan import MODEL_REPORT_SCHEMA
                    modeling_report_path = str(next_dir / "modeling_report.json")
                    schema_path = project / next_dir / "modeling_report_schema.json"
                    write_json(schema_path, MODEL_REPORT_SCHEMA)
                    report_instruction = f"同时输出 {modeling_report_path}，遵循 {schema_path}，逐项说明每个初始化物体与未解释区域对应的候选对象、修正或排除理由及原图依据。"
                note = f"""独立审核未通过，全部类别的问题都交给你在Blender中逐物体精细重建和修复。计划、SAM3、DA3、TSDF仅为初始化参考，不再退回计划或分割流程。按category处理：优先修spatial_structure的所有物体及部件的凹凸/内外、相对前后/朝向/厚度、连接与穿插，明确参照对象或表面，用有视差的原图和遮挡核对，不能仅凭阴影或单个深度预测定凹凸；证据不足明确记录。silhouette修轮廓几何，texture_alignment修UV/投影，lighting修灯光/曝光；材质本色问题单独处理。先核对相机，原因不确定时先补诊断；每条按指定视角及验收条件修复，不能把光影画进底色。先实际查看 {previous}、{frozen / 'inspection.json'} 和 {frozen / 'editability.json'} 中图像，再按原始多视角处理漏物体、错误边界、几何、材质与编辑问题。允许细化物体部件和层级，必须覆盖原有物体；不要把改名/多分几组当作真实外观完成。所有原候选与初始化/审核证据保持不变；新资产、脚本放 {next_dir}。输出 {candidate_path}（revision={world['revision'] + 1}）和 {receipt_path}，使用 build_world(scene=candidate, render=True)。{report_instruction}保持原世界坐标和固定验证相机，可补充检查视角。不要修改 world/scene.json，不发布，不修改仓库或审核记录。父子对象与本地原点必须通过实际移动旋转测试。物理延后。优先修复这些问题：{json.dumps(review['issues'][:5], ensure_ascii=False)}。检查其余旧问题，不能遗漏。"""
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
    parser.add_argument("--plan", help="Project-relative object inventory; requires --segments and --geometry")
    parser.add_argument("--segments", help="Segmentation manifest bound to the plan")
    parser.add_argument("--geometry", help="Per-object geometry manifest bound to the plan and masks")
    parser.add_argument("--modeling-report", help="Candidate object/region handling report; defaults to candidate directory/modeling_report.json")
    parser.add_argument("--previous-review", help="Carry previous issue IDs across controller runs")
    parser.add_argument("--feedback-id", help="Wait for this already submitted builder task before freezing")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--review-model", default="gpt-6.1-sol")
    parser.add_argument("--requested-review-model", default="gpt-6.1-sol")
    parser.add_argument("--max-rounds", type=int, choices=range(1, 4), default=3)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
