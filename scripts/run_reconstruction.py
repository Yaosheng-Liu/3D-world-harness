#!/usr/bin/env python3
"""Known-camera object planning -> SAM 3 -> geometry -> modeler -> critic.

Runtime commands are explicit JSON argument lists, never shell strings. Heavy
models may live in separate containers; the workbench remains lightweight.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from reconstruction_plan import (PLAN_SCHEMA, observations, project_file, read_json,
                                 unresolved_objects, validate_plan, validate_segments, validate_geometry, write_json)
from world_build import file_hash
from world_model import WorldStore, scene_hash
from review_reconstruction import Workbench, run as review_candidate


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def codex_home(path):
    previous = os.environ.get("CODEX_HOME")
    if path:
        os.environ["CODEX_HOME"] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = previous


def contact_sheets(project, directory):
    """Every training view appears in a labelled sheet; originals stay accessible."""
    from PIL import Image, ImageDraw
    frames = observations(project)["train"]
    paths = []
    for start in range(0, len(frames), 8):
        sheet = Image.new("RGB", (1280, 840), "#f3f4f6")
        draw = ImageDraw.Draw(sheet)
        for offset, row in enumerate(frames[start:start + 8]):
            x, y = offset % 4 * 320, offset // 4 * 420
            with Image.open(project_file(project, row["path"])) as source:
                image = source.convert("RGB")
                image.thumbnail((312, 380))
                sheet.paste(image, (x + 4, y + 30))
            draw.text((x + 8, y + 8), f"source_{row['index']}  frame={row['index']}", fill="black")
        path = directory / f"references_{start // 8:02d}.jpg"
        sheet.save(path, quality=90)
        paths.append(path)
    return paths


def run_planner(project, directory, runtime, previous=None, feedback=None):
    directory.mkdir(parents=True, exist_ok=False)
    source_hash = file_hash(project_file(project, "observations/manifest.json"))
    schema = directory / "schema.json"
    write_json(schema, PLAN_SCHEMA)
    prompt = f"""You are the scene-planning agent for 3D-world harness. Read-only analysis; do not modify files, build, publish or call write services. Output the object-plan JSON schema.
Goal: reconstruct the observed real scene with individually editable objects. Known camera calibration is supplied; physics and unknown-camera estimation are deferred.
Project: {project}
Prepared training images: {project / 'observations/manifest.json'}; source_manifest_sha256 must be exactly {source_hash}.
Attached contact sheets show EVERY training image, labelled with its original frame_index. Open full-resolution originals to inspect objects and locate accurate normalized [x0,y0,x1,y1] boxes; do not estimate boxes in contact-sheet coordinates. Never use heldout images for segmentation/planning. Existing world/scene.json is context, not ground truth. Read the original images independently to discover omissions, including peripheral objects and parts swallowed by larger objects.
Produce stable ASCII IDs, names, object/part/structure hierarchy, priority, short English SAM3 segmentation_prompt, per-object observations with frame_index and box, uncertainty and concrete acceptance_criteria. Cite at least 2 distinct good views for fuse objects, preferably 3-5 spanning viewpoints; one-view objects may use explicit model strategy with honest uncertainty. Do not confuse repeated instances across views. Groups are task-only organizational nodes, with strategy group; use a real frame/cabinet object as parent for editable parts when possible. Each pixel belongs to the most specific visible instance, not both parent and child.
Use Chinese for user-facing names, summaries, descriptions, uncertainty and acceptance criteria; retain ASCII IDs and short English segmentation prompts. Higher priority numbers mean earlier modeling.
geometry_strategy=fuse uses shared DA3 and per-object TSDF; model explicitly requests Blender modeling from references, without claiming measured geometry; group creates no mesh. List every important visible editable object. Unexplained visible regions remain in unexplained_regions until identified or evidenced exclusions; they block automatic modeling. Occluded unseen objects must not be invented as observations.
Previous plan: {previous or 'none'}.
Evidence/review feedback: {json.dumps(feedback, ensure_ascii=False) if feedback else 'none'}.
Preserve every previous object ID, including prior excluded IDs. If a candidate was a duplicate/false detection, move its ID to excluded_objects with a concrete reason and source frame_indices; never silently delete a SAM miss. A failed SAM result means segmentation failed, not that an object does not exist. Revise bad boxes, add clearer views, clarify prompts. If reliable masks/geometric support remain unavailable but image evidence supports existence, explicitly select model strategy and describe the limitation. No automatic substitution of SAM2, fabricated masks or generic placeholder assets.
Keep the plan concise and executable; it is evidence for the builder and independent critic, not a claim of completed reconstruction.
"""
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    role = runtime["planner"]
    output = directory / "plan.json"
    command = [role["codex"], "exec", "-C", str(project), "--skip-git-repo-check", "-s", "read-only",
               "-c", 'approval_policy="never"', "-m", role["model"],
               "-c", f'model_reasoning_effort="{role["effort"]}"', "--json",
               "--output-schema", str(schema), "--output-last-message", str(output)]
    for image in contact_sheets(project, directory):
        command.extend(["--image", str(image)])
    command.append("-")
    with codex_home(role.get("home")), (directory / "events.jsonl").open("w") as events, (directory / "stderr.log").open("w") as errors:
        subprocess.run(command, input=prompt, text=True, stdout=events, stderr=errors,
                       check=True, timeout=runtime.get("planner_timeout", 1800))
    plan = validate_plan(project, read_json(output), read_json(project_file(project, previous)) if previous else None)
    # Detect source replacement while the independent planner was reading it.
    observations(project)
    if source_hash != file_hash(project_file(project, "observations/manifest.json")):
        raise ValueError("observations changed while planning")
    thread_id = next((json.loads(line)["thread_id"] for line in (directory / "events.jsonl").read_text().splitlines()
                      if json.loads(line).get("type") == "thread.started"), None)
    write_json(directory / "receipt.json", {"model": role["model"], "effort": role["effort"],
               "thread_id": thread_id, "plan_sha256": file_hash(output), "source_manifest_sha256": source_hash})
    return str(output.relative_to(project)), plan


def run_tool(name, runtime, project, run_dir, plan_path, output, segments_path=None):
    command = runtime[name + "_command"]
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError(f"{name}_command must be a nonempty JSON argument list")
    values = {"project": str(project), "run_dir": str(run_dir), "run_relative": str(run_dir.relative_to(project)),
              "plan": plan_path, "output": output, "segments": segments_path or "", "repo": str(ROOT)}
    arguments = [arg.format_map(values) for arg in command]
    log = project / (output + ".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as stream:
        subprocess.run(arguments, check=True, stdout=stream, stderr=subprocess.STDOUT,
                       timeout=runtime.get("tool_timeout", 3600))
    return str(Path(output) / ("segments.json" if name == "segmentation" else "geometry.json"))


def modeling_prompt(project, status, candidate, receipt, previous_review=None):
    return f"""执行本轮3D-world harness真实场景建模任务。物理暂缓。先看原图、物体清单、分割和逐物体几何证据，再在Blender建模。
计划：{project / status['plan_path']}
分割记录：{project / status['segments_path']}
观测几何：{project / status['geometry_path']}
上轮审核：{previous_review or '无'}
必须完成计划中每个非group物体，严格保留稳定ID；保留有几何的父子关系。group只是任务分组，可省略没有几何的group节点并连接最近实体祖先。几何/掩码只是证据，不能把TSDF碎片或裁剪照片当成完成的可编辑模型。strategy=model的物体即使SAM漏检也必须根据原始多视角单独建模；未观测面明确标为estimated。用原始图补足边界、背面、底面和遮挡，限制纹理来源，不能把枕头/灯具烘焙到邻物体。没有证据的细节不得声称实测。
按priority建模并检查移动/旋转：移动物体后旧位置不能残留重复几何或纹理，墙地面不能被一起带走。材质、原点、层级与轮廓都需要检查。独立审核将逐项检查所有原图、计划覆盖和旧问题。
当前世界版本为{status['base_world_revision']}。输出完整候选 {candidate}，revision={status['base_world_revision']+1}；所有新脚本、资产放候选同目录。用build_world(scene=candidate,render=True)构建，将完整receipt写到{receipt}。保持原坐标、全部已有验证相机和原始来源。不要修改world/scene.json、已发布文件、计划/分割/几何证据、审核记录或仓库代码，不要发布。候选由独立审核通过后统一发布。
如发现计划遗漏/身份错误，记录到候选目录modeling_report.json，不能靠删对象或伪造证据过关；审核后由计划Agent修正。
"""


def load_evidence(project, status):
    plan = validate_plan(project, read_json(project_file(project, status["plan_path"])))
    segments = validate_segments(project, status["plan_path"], read_json(project_file(project, status["segments_path"])))
    geometry = validate_geometry(project, status["plan_path"], status["segments_path"],
                                 read_json(project_file(project, status["geometry_path"])))
    return plan, segments, geometry, unresolved_objects(plan, segments, geometry)


def run(options):
    project = options.project.resolve(strict=True)
    runtime = read_json(options.runtime)
    observations(project)
    directory = project / "object_pipeline"
    directory.mkdir(exist_ok=True)
    with (directory / ".run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if options.resume:
            status = read_json(project_file(project, Path("object_pipeline") / options.resume / "status.json"))
            if status.get("accepted"):
                raise ValueError("this run has already been accepted; start a new run for new work")
            run_dir = project / "object_pipeline" / options.resume
            resume_pending = bool((status.get("feedback_id") and status.get("reviewed_feedback_id") != status["feedback_id"])
                                  or (status.get("builder_request") and not status.get("feedback_id")))
            start_round = status["round"] if resume_pending else status["round"] + 1
        else:
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
            run_dir = directory / run_id
            run_dir.mkdir()
            world = WorldStore(project).read()
            status = {"run_id": run_id, "created_at": now(), "base_world_revision": world["revision"],
                      "base_world_hash": scene_hash(world), "accepted": False, "completed_stages": [],
                      "planner": {key: runtime["planner"][key] for key in ("model", "effort")},
                      "builder": {"model": "gpt-6-astra", "effort": "high"},
                      "reviewer": {"model": runtime["reviewer"]["model"], "effort": "ultra"},
                      "physics": "deferred", "round": 0}
            start_round = 1
            resume_pending = False
        status.update(pid=os.getpid(), error=None, reason=None)

        def update(stage, **fields):
            status.update(stage=stage, updated_at=now(), **fields)
            write_json(run_dir / "status.json", status)
            write_json(directory / "status.json", status)
            print(json.dumps({"run_id": status["run_id"], "stage": stage, "round": status["round"], **fields}, ensure_ascii=False), flush=True)

        previous_review = status.get("previous_review")
        try:
            if scene_hash(WorldStore(project).read()) != status["base_world_hash"]:
                raise ValueError("published world changed; start a new run from the current revision")
            for number in range(start_round, start_round + options.max_rounds):
                round_dir = run_dir / f"round_{number:02d}"
                round_dir.mkdir(exist_ok=resume_pending)
                status["round"] = number
                feedback = read_json(project_file(project, previous_review)) if previous_review else None
                # A failed geometry worker must not repeat the completed paid
                # planner/SAM stages. Retry only geometry in a fresh directory.
                if (options.resume and number == start_round and not resume_pending
                        and status.get("segments_path") and not status.get("geometry_path")
                        and options.stop_after in {"geometry", "review"}):
                    plan_path = status["plan_path"]
                    plan = validate_plan(project, read_json(project_file(project, plan_path)))
                    segments = validate_segments(project, plan_path, read_json(project_file(project, status["segments_path"])))
                    update("geometry")
                    geometry_path = run_tool("geometry", runtime, project, run_dir, plan_path,
                                             str((round_dir / "geometry_resume").relative_to(project)), status["segments_path"])
                    geometry = validate_geometry(project, plan_path, status["segments_path"], read_json(project_file(project, geometry_path)))
                    tasks = unresolved_objects(plan, segments, geometry)
                    update("geometry", geometry_path=geometry_path, completed_stages=["planning", "segmenting", "geometry"])
                    if options.stop_after == "geometry":
                        blocked = any(task["blocking"] for task in tasks)
                        update("needs_revision" if blocked else "ready_for_modeling", reason=
                               "物体几何已续跑；仍有明确的证据/表示问题待计划修订。" if blocked
                               else "物体证据已准备；尚未建模或通过质量审核。")
                        return status
                needs_plan = not status.get("geometry_path") or any(
                    issue.get("target") in {"planning", "segmentation"} for issue in (feedback or {}).get("issues", []))
                if status.get("geometry_path"):
                    _, _, _, tasks = load_evidence(project, status)
                    needs_plan = needs_plan or any(task["blocking"] for task in tasks)
                if resume_pending:
                    if not status.get("geometry_path") or any(task["blocking"] for task in tasks):
                        raise ValueError("submitted builder task has unresolved or missing evidence; inspect its saved feedback")
                    needs_plan = False
                if needs_plan:
                    previous_plan = status.get("plan_path")
                    for attempt in range(2):
                        update("planning", completed_stages=[], segments_path=None, geometry_path=None)
                        plan_path, plan = run_planner(project, round_dir / f"plan_{attempt + 1}", runtime,
                                                      previous_plan, feedback)
                        update("planning", plan_path=plan_path, summary=plan["summary"], completed_stages=["planning"])
                        if options.stop_after == "plan":
                            update("planned", reason="计划已保存；分割尚未运行。")
                            return status
                        update("segmenting")
                        segments_path = run_tool("segmentation", runtime, project, run_dir, plan_path,
                                                 str((round_dir / f"segments_{attempt + 1}").relative_to(project)))
                        segments = validate_segments(project, plan_path, read_json(project_file(project, segments_path)))
                        update("segmenting", segments_path=segments_path, completed_stages=["planning", "segmenting"])
                        if options.stop_after == "segment":
                            update("segmented", reason="分割记录已保存；物体几何尚未运行。")
                            return status
                        update("geometry")
                        geometry_path = run_tool("geometry", runtime, project, run_dir, plan_path,
                                                 str((round_dir / f"geometry_{attempt + 1}").relative_to(project)), segments_path)
                        geometry = validate_geometry(project, plan_path, segments_path, read_json(project_file(project, geometry_path)))
                        tasks = unresolved_objects(plan, segments, geometry)
                        update("geometry", geometry_path=geometry_path, completed_stages=["planning", "segmenting", "geometry"])
                        if not any(task["blocking"] for task in tasks):
                            break
                        previous_plan = plan_path
                        feedback = {"unresolved": tasks, "segments_path": segments_path, "geometry_path": geometry_path,
                                    "previous_review": previous_review}
                    if any(task["blocking"] for task in tasks):
                        update("needs_revision", reason="两次计划/分割后仍有证据缺口；保留物体清单，未启动建模。")
                        return status
                _, _, _, tasks = load_evidence(project, status)
                if any(task["blocking"] for task in tasks):
                    raise ValueError("unresolved object evidence cannot enter modeling")
                update("ready_for_modeling", reason="物体证据已准备；尚未建模或通过质量审核。")
                if options.stop_after == "geometry":
                    return status
                if not options.url:
                    raise ValueError("--url is required for modeling and review")
                workbench = Workbench(options.url)
                current = workbench.call("/api/workspace/state")
                if Path(current["project_dir"]).resolve() != project:
                    raise ValueError("workbench URL belongs to another project")
                if scene_hash(WorldStore(project).read()) != status["base_world_hash"]:
                    raise ValueError("world changed while preparing evidence")
                model_dir = round_dir / "model"
                if resume_pending:
                    candidate, receipt, feedback_id = status["candidate_path"], status["receipt_path"], status.get("feedback_id")
                    if not feedback_id:
                        request = status["builder_request"]
                        saved = workbench.call(f"/api/sessions/{request['session_id']}/feedback")["items"]
                        matches = [item for item in saved if item.get("idempotency_key") == request["key"]]
                        if len(matches) != 1 or matches[0]["note"] != (model_dir / "prompt.txt").read_text(encoding="utf-8"):
                            raise RuntimeError("builder submission is uncertain; inspect the saved request before resubmitting")
                        feedback_id = matches[0]["feedback_id"]
                else:
                    model_dir.mkdir()
                    candidate = str((model_dir / "candidate.json").relative_to(project))
                    receipt = str((model_dir / "build_receipt.json").relative_to(project))
                    prompt = modeling_prompt(project, status, candidate, receipt, previous_review)
                    (model_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                    key = status["run_id"] + f"-model-{number}"
                    update("modeling", reason=None, candidate_path=candidate, receipt_path=receipt,
                           feedback_id=None, builder_request={"key": key, "session_id": current["session_id"]})
                    feedback_id = workbench.repair(prompt, key)
                update("modeling", feedback_id=feedback_id)
                workbench.wait_builder(feedback_id)
                update("reviewing", completed_stages=["planning", "segmenting", "geometry", "modeling"])
                role = runtime["reviewer"]
                review_options = SimpleNamespace(project=project, url=options.url, candidate=candidate, receipt=receipt,
                    feedback_id=None, codex=role["codex"], blender=runtime.get("blender", "blender"),
                    review_model=role["model"], requested_review_model="gpt-6.1-sol", max_rounds=1,
                    plan=status["plan_path"], segments=status["segments_path"], geometry=status["geometry_path"],
                    previous_review=previous_review)
                with codex_home(role.get("home")):
                    reviewed = review_candidate(review_options)
                previous_review = str(Path(reviewed["review_directory"]) / "review.json")
                update("reviewing", previous_review=previous_review, review_run_id=reviewed["run_id"],
                       reviewed_feedback_id=feedback_id)
                resume_pending = False
                if reviewed["accepted"]:
                    update("accepted", accepted=True, summary=reviewed["summary"],
                           completed_stages=["planning", "segmenting", "geometry", "modeling", "reviewing", "publishing"])
                    return status
                feedback = read_json(project_file(project, previous_review))
                if feedback["verdict"] == "blocked":
                    break
            update("needs_revision", reason="本轮审核未通过；问题与物体证据已保存，可显式续跑。")
            return status
        except (Exception, KeyboardInterrupt) as exc:
            message = (f"当前阶段执行失败（退出码 {exc.returncode}）；详见本轮运行日志。"
                       if isinstance(exc, subprocess.CalledProcessError) else str(exc))
            update("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=message)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True, help="Local runtime JSON with explicit commands and Codex roles")
    parser.add_argument("--url", help="Project's existing workbench URL; required after geometry")
    parser.add_argument("--stop-after", choices=("plan", "segment", "geometry", "review"), default="review")
    parser.add_argument("--max-rounds", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--resume", help="Continue a saved run, preserving object IDs and prior issues, in a new round")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
