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
from reconstruction_plan import (PLAN_SCHEMA, MODEL_REPORT_SCHEMA, observations, project_file, read_json,
                                 unresolved_objects, validate_plan, validate_segments, validate_geometry, write_json,
                                 ModelingReportError, read_modeling_report, validate_modeling_report)
from world_build import file_hash
from world_model import WorldStore, scene_hash
from review_reconstruction import Workbench, builder_role, run as review_candidate


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


def run_planner(project, directory, runtime):
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
geometry_strategy=fuse initializes shared DA3/per-object TSDF observations; model starts from original-image evidence; group creates no mesh. List every important visible editable object. Preserve ambiguous visible regions in unexplained_regions as modeling tasks. These uncertainties do not prevent the modeler from beginning detailed work. Occluded unseen objects must not be invented as observations.
This is one-time scene initialization. The modeler owns all subsequent refinement, including omissions, wrong instance associations, split/merge corrections and geometry gaps. The independent critic sends every issue directly to that modeler. The plan is a hypothesis and may be corrected with image evidence in the modeler's report. Do not silently drop an observed object because SAM may miss it, fabricate masks, or assume generic placeholder assets are sufficient.
Keep this initial plan concise and executable; it is evidence for detailed modeling, not a claim of completed reconstruction.
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
    plan = validate_plan(project, read_json(output))
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
    diagnostic = status.get("builder_diagnostic_path")
    report_diagnostic = (f"{project / diagnostic}\n{read_json(project_file(project, diagnostic))['message']}"
                         if diagnostic else "无")
    return f"""执行本轮3D-world harness真实场景建模任务。物理暂缓。先看原图、物体清单、分割和逐物体几何证据，再在Blender建模。
返修起点：{status.get('candidate_path') or '初始化证据'}。已有候选作为修改起点，保留正确部分；以原图和本轮审核为准修正。
计划：{project / status['plan_path']}
分割记录：{project / status['segments_path']}
观测几何：{project / status['geometry_path']}
上轮审核：{previous_review or '无'}
审核目录inspection.json中的overlay_paths是同相机的原图/模型50%叠图；结合纯渲染和原图处理审核指出的重影、轮廓、部件位置、遮挡和纹理错位。叠图里的照片细节不代表模型已具备该细节，不能仅凭混合图验收。正式检查由审核Agent负责。
按问题类别返工：先处理spatial_structure（空间结构错误），覆盖每个物体内部、部件之间及物体之间的关系，以相关对象/部件/表面及局部方向为参照，用有平移视差的多视角原图、遮挡和可信深度核对凹凸/内外、前后/朝向/厚度、连接/开口、穿插或悬空；曲面不强行套用墙面平面，证据中注明相关对象ID，不能只凭明暗或DA3/TSDF自证凹凸；先核对相机/坐标，证据不足标明不确定性，不能靠贴图或灯光掩盖结构错误。silhouette（轮廓错误）修几何/比例/褶皱；texture_alignment（纹理错位）修UV、来源视角与遮挡投影；lighting（光照偏差）修灯光及曝光，不能把阴影或高光画进底色补偿。appearance中的材质问题独立处理。先核对相机；原因不确定时按审核要求补诊断，不盲目同时修改几何、材质和灯光。每条反馈按其物体、视角、区域与验收条件处理，保留问题ID供审核复验。
建模报告格式返工诊断（不是独立审核）：{report_diagnostic}
前面的计划、SAM、DA3/TSDF都是初始化。你负责逐个物体精细建模，并负责所有审核返工，包括漏物体、错误拆分/实例对应、分割边界和几何缺口，不等待计划Agent或前处理替你解决。默认保留稳定ID；如原清单认错、需拆分/合并或调整层级，可依据原图纠正，在modeling_report.json中明确映射，不可静默删除。group只是任务分组，可省略无几何的group节点。不能把TSDF碎片或裁剪照片当作已完成模型；SAM漏检或几何超限的物体仍须按原图建模，必要时自己调用工具修正局部证据。未观测面标为estimated。限制纹理来源，不能把枕头/灯具烘焙到邻物体。
按priority逐组建模。每完成一组有意义的物体（例如床架和床头、床品、两侧柜灯），就用build_world生成完整当前候选的Blender/GLB，render=False即可；随后用workspace_preview_world传入构建receipt.json、当前显示版本和简短完成说明，更新工具台。中间预览允许尚未建完，只展示实际完成的几何，不伪造其余部分；保持稳定ID，并在下一次预览保留已完成对象。预览不代表验收，不修改world/scene.json。
你负责正常构建所需的基本有效性；正式的移动/旋转、隐藏残留、完整性和原图质量检查交给独立审核阶段，不重复执行整套验收。独立审核会核对原图、物体处理覆盖和旧问题。
当前世界版本为{status['base_world_revision']}。输出完整候选 {candidate}，revision={status['base_world_revision']+1}；所有新脚本、资产放候选同目录。用build_world(scene=candidate,render=True)构建，将完整receipt写到{receipt}。保持原坐标、全部已有验证相机和原始来源。不要修改world/scene.json、已发布文件、计划/分割/几何证据、审核记录或仓库代码，不要发布。候选由独立审核通过后统一发布。
另外输出候选同目录的modeling_report.json，严格遵循该目录modeling_report_schema.json。报告绑定初始plan文件SHA-256，objects逐项记录每个初始非group物体对应的scene_ids；regions逐项处理初始unexplained_regions的索引。resolution为modeled/excluded/unresolved，每条给reason和原始frame_indices依据。frame_indices只填写observations/manifest.json中不重复的train索引，并至少引用该初始物体或区域的一帧；其他审核视角可以在reason说明。拆合可显式映射一对多/多对一；排除必须有证据，不能凭分割失败否认物体。报告只说明如何处理初始化，不代表你可以自行验收；所有问题由独立审核复验。原初始化与旧审核证据保持不变。
"""


def load_evidence(project, status):
    plan = validate_plan(project, read_json(project_file(project, status["plan_path"])))
    segments = validate_segments(project, status["plan_path"], read_json(project_file(project, status["segments_path"])))
    geometry = validate_geometry(project, status["plan_path"], status["segments_path"],
                                 read_json(project_file(project, status["geometry_path"])))
    return plan, segments, geometry, unresolved_objects(plan, segments, geometry)


def initialize(project, run_dir, round_dir, runtime, status, update, stop_after):
    """Run each missing initialization stage once; leave gaps to the modeler."""
    if not status.get("plan_path"):
        update("planning", completed_stages=[])
        plan_path, plan = run_planner(project, round_dir / "plan", runtime)
        update("planning", plan_path=plan_path, summary=plan["summary"], completed_stages=["planning"])
    else:
        validate_plan(project, read_json(project_file(project, status["plan_path"])))
    if stop_after == "plan":
        update("planned", reason="初始化计划已保存；尚未精细建模。")
        return False
    if not status.get("segments_path"):
        update("segmenting")
        path = run_tool("segmentation", runtime, project, run_dir, status["plan_path"],
                        str((round_dir / "segments").relative_to(project)))
        validate_segments(project, status["plan_path"], read_json(project_file(project, path)))
        update("segmenting", segments_path=path, completed_stages=["planning", "segmenting"])
    else:
        validate_segments(project, status["plan_path"], read_json(project_file(project, status["segments_path"])))
    if stop_after == "segment":
        update("segmented", reason="初始化分割已保存；尚未精细建模。")
        return False
    if not status.get("geometry_path"):
        update("geometry")
        path = run_tool("geometry", runtime, project, run_dir, status["plan_path"],
                        str((round_dir / "geometry").relative_to(project)), status["segments_path"])
        validate_geometry(project, status["plan_path"], status["segments_path"], read_json(project_file(project, path)))
        update("geometry", geometry_path=path, completed_stages=["planning", "segmenting", "geometry"])
    _, _, _, todos = load_evidence(project, status)
    update("ready_for_modeling", modeling_todo_count=len(todos), completed_stages=["planning", "segmenting", "geometry"],
           reason=f"初始证据可用；{len(todos)}项证据缺口交给建模 Agent 精细处理，尚未通过质量审核。")
    return stop_after != "geometry"


def review_current(project, options, runtime, status, *, publish):
    role = runtime["reviewer"]
    review_options = SimpleNamespace(
        project=project, url=options.url, candidate=status["candidate_path"], receipt=status["receipt_path"],
        feedback_id=None, codex=role["codex"], blender=runtime.get("blender", "blender"),
        review_model=role["model"], requested_review_model="gpt-6.1-sol", max_rounds=1,
        review_timeout=runtime.get("review_timeout", 2400),
        publish_after_review=publish, plan=status["plan_path"], segments=status["segments_path"],
        geometry=status["geometry_path"],
        modeling_report=str(Path(status["candidate_path"]).parent / "modeling_report.json"),
        previous_review=status.get("previous_review"))
    with codex_home(role.get("home")):
        return review_candidate(review_options)


def run(options):
    feedback_stop = options.stop_after == "feedback-modeling"
    project = options.project.resolve(strict=True)
    runtime = read_json(options.runtime)
    observations(project)
    directory = project / "object_pipeline"
    directory.mkdir(exist_ok=True)
    with (directory / ".run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if options.resume:
            status = read_json(project_file(project, Path("object_pipeline") / options.resume / "status.json"))
            if (feedback_stop or status.get("repair_limit")) and status.get("feedback_modeling_finished"):
                return status
            if status.get("accepted"):
                raise ValueError("this run has already been accepted; start a new run for new work")
            run_dir = project / "object_pipeline" / options.resume
            checked_feedback = status.get("checked_feedback_id", status.get("reviewed_feedback_id"))
            resume_pending = bool((status.get("feedback_id") and checked_feedback != status["feedback_id"])
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
            if getattr(options, "repair_from", None):
                source = read_json(project_file(project, Path("object_pipeline") / options.repair_from / "status.json"))
                completed_repair = (source["stage"] == "failed" and source.get("repairs_completed", 0) > 0
                                    and source.get("pending_review") and source.get("feedback_id")
                                    and source.get("checked_feedback_id") == source["feedback_id"])
                if (source["stage"] != "modeled" and not completed_repair) or source.get("accepted"):
                    raise ValueError("--repair-from requires a stopped, unpublished modeled candidate")
                for key in ("base_world_revision", "base_world_hash", "plan_path", "segments_path", "geometry_path",
                            "candidate_path", "receipt_path", "previous_review", "completed_stages"):
                    status[key] = source[key]
                status.update(source_run_id=source["run_id"], repair_limit=options.repair_cycles,
                              repairs_completed=0, reviews_completed=0, pending_review=True)
            start_round = 1
            resume_pending = False
        repair_limit = status.get("repair_limit", 0)
        status.update(pid=os.getpid(), error=None, reason=None)

        def update(stage, **fields):
            status.update(stage=stage, updated_at=now(), **fields)
            write_json(run_dir / "status.json", status)
            write_json(directory / "status.json", status)
            print(json.dumps({"run_id": status["run_id"], "stage": stage, "round": status["round"], **fields}, ensure_ascii=False), flush=True)

        previous_review = status.get("previous_review")
        try:
            if options.url:
                status["builder"] = builder_role(Workbench(options.url).call("/api/workspace/state"))
            if scene_hash(WorldStore(project).read()) != status["base_world_hash"]:
                raise ValueError("published world changed; start a new run from the current revision")
            rounds = repair_limit - status["repairs_completed"] if repair_limit else options.max_rounds
            for number in range(start_round, start_round + rounds):
                if repair_limit and status.get("review_in_progress"):
                    raise RuntimeError("review outcome is uncertain; inspect the saved reconstruction run before resuming")
                if repair_limit and status["pending_review"]:
                    load_evidence(project, status)
                    update("reviewing", review_in_progress=True,
                           reason=f"本次审核 {status['reviews_completed'] + 1}/{repair_limit}；审核后返修，不发布。")
                    reviewed = review_current(project, options, runtime, status, publish=False)
                    previous_review = str(Path(reviewed["review_directory"]) / "review.json")
                    update("reviewing", previous_review=previous_review, review_run_id=reviewed["run_id"],
                           reviews_completed=status["reviews_completed"] + 1,
                           pending_review=False, review_in_progress=False)
                round_dir = run_dir / f"round_{number:02d}"
                round_dir.mkdir(exist_ok=resume_pending)
                status["round"] = number
                if resume_pending or repair_limit:
                    load_evidence(project, status)
                elif not initialize(project, run_dir, round_dir, runtime, status, update, options.stop_after):
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
                    write_json(model_dir / "modeling_report_schema.json", MODEL_REPORT_SCHEMA)
                    prompt = modeling_prompt(project, status, candidate, receipt, previous_review)
                    (model_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                    key = status["run_id"] + f"-model-{number}"
                    update("modeling", reason=None, candidate_path=candidate, receipt_path=receipt,
                           feedback_id=None, builder_request={"key": key, "session_id": current["session_id"]})
                    feedback_id = workbench.repair(prompt, key)
                update("modeling", feedback_id=feedback_id)
                workbench.wait_builder(feedback_id, preview_project=project)
                # Evidence and candidate failures stay fatal; only builder-report
                # format errors return to this builder without inventing a review.
                observations(project)
                load_evidence(project, status)
                scene = read_json(project_file(project, candidate))
                report_path = str((model_dir / "modeling_report.json").relative_to(project))
                try:
                    report = read_modeling_report(project, report_path)
                    validate_modeling_report(project, status["plan_path"], scene, report)
                except ModelingReportError as exc:
                    diagnostic = model_dir / "builder_diagnostic.json"
                    write_json(diagnostic, {"kind": "modeling_report", "feedback_id": feedback_id,
                        "candidate_path": candidate, "modeling_report_path": report_path, "message": str(exc)})
                    update("needs_revision", builder_diagnostic_path=str(diagnostic.relative_to(project)),
                           checked_feedback_id=feedback_id, reason="建模处理报告格式不完整，已保存返工诊断。")
                    if repair_limit:
                        raise
                    if feedback_stop and status.get("feedback_stop_review_path"):
                        update("needs_revision", feedback_modeling_finished=True,
                               reason="反馈建模已结束；处理报告需修正，按本轮限制停止，不再审核或派单。")
                        return status
                    resume_pending = False
                    continue
                if repair_limit:
                    completed = status["repairs_completed"] + 1
                    update("modeled", repairs_completed=completed, pending_review=True,
                           checked_feedback_id=feedback_id, builder_diagnostic_path=None,
                           feedback_modeling_finished=completed == repair_limit,
                           reason=f"本次已完成 {completed}/{repair_limit} 次审核及返修。" +
                                  ("按要求停止，最终返修尚未复核或发布。" if completed == repair_limit else "准备下一次审核。"))
                    if completed == repair_limit:
                        return status
                    resume_pending = False
                    continue
                if feedback_stop and status.get("feedback_stop_review_path"):
                    update("modeled", feedback_modeling_finished=True,
                           reason="已完成一次审核后的反馈建模；按用户要求停止，最终候选尚未复核或发布。",
                           completed_stages=["planning", "segmenting", "geometry", "modeling"])
                    return status
                update("reviewing", builder_diagnostic_path=None,
                       completed_stages=["planning", "segmenting", "geometry", "modeling"])
                reviewed = review_current(project, options, runtime, status, publish=not feedback_stop)
                previous_review = str(Path(reviewed["review_directory"]) / "review.json")
                update("reviewing", previous_review=previous_review, review_run_id=reviewed["run_id"],
                       reviewed_feedback_id=feedback_id, checked_feedback_id=feedback_id)
                if feedback_stop:
                    update("reviewing", feedback_stop_review_path=previous_review)
                resume_pending = False
                if reviewed["accepted"]:
                    update("accepted", accepted=True, summary=reviewed["summary"],
                           completed_stages=["planning", "segmenting", "geometry", "modeling", "reviewing", "publishing"])
                    return status
                feedback = read_json(project_file(project, previous_review))
                if feedback["verdict"] == "blocked" and not feedback_stop:
                    break
            reason = ("建模处理报告需要修正；返工诊断已保存，可显式续跑。" if status.get("builder_diagnostic_path")
                      else "本轮审核未通过；问题与物体证据已保存，可显式续跑。")
            update("needs_revision", reason=reason)
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
    parser.add_argument("--stop-after", choices=("plan", "segment", "geometry", "review", "feedback-modeling"), default="review",
                        help="feedback-modeling runs at most one review, then stops after its modeling feedback is handled")
    parser.add_argument("--max-rounds", type=int, choices=range(1, 4), default=3)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--resume", help="Continue a saved run; bounded repair batches retain their original limit")
    source.add_argument("--repair-from", help="Start a bounded review-then-repair batch from a stopped modeled run")
    parser.add_argument("--repair-cycles", type=int, choices=range(1, 4), default=2,
                        help="Number of NEW review/repair pairs with --repair-from; stops after the last repair without publishing")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
