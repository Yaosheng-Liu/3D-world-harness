"""Fresh headless trials using the harness's existing planner and review core."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from common import (REPO, bind_candidate_sources, camera_manifest, cli_usage, digest,
                    matrix, model_handoff, read, thread_usage, write)

sys.path[:0] = [str(REPO / "backend"), str(REPO / "scripts")]
from reconstruction_plan import MODEL_REPORT_SCHEMA, PLAN_SCHEMA
from reconstruction_appearance import appearance_instruction
from reconstruction_review import freeze_candidate, review_schema, validate_review, validate_fixed_views, verify_files
from review_reconstruction import review_prompt
from run_reconstruction import run_planner
from world_build import build_world
from world_model import scene_hash, source_paths, validate_scene


def environment(runtime, home):
    env = dict(os.environ, CODEX_HOME=str(home), HTTP_PROXY=runtime["proxy"],
               HTTPS_PROXY=runtime["proxy"], NO_PROXY="localhost,127.0.0.1,::1")
    return env


def agent(project, directory, runtime, role, home, prompt, *, images=(), thread=None, schema=None,
          previous_usage=None, timeout=1800):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "prompt.txt").write_text(prompt)
    final = directory / "response.json" if schema else directory / "response.txt"
    argv = [runtime["codex"], "exec", "--ignore-user-config", "--skip-git-repo-check", "--json",
            "-C", str(project), "-s", "read-only" if schema else "workspace-write",
            "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=true",
            "-m", role["model"], "-c", f'model_reasoning_effort="{role["effort"]}"',
            "--output-last-message", str(final)]
    if schema:
        write(directory / "schema.json", schema)
        argv += ["--output-schema", str(directory / "schema.json")]
    if thread:
        argv += ["resume"]
    for image in images:
        argv += ["--image", str(image)]
    argv += [thread, "-"] if thread else ["-"]
    unit = "worldharness-" + uuid.uuid4().hex
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit", unit,
            "--property=TimeoutStopSec=5s", "--", *argv]
    started = time.time()
    exit_code, error = None, None
    events = directory / "events.jsonl"
    with events.open("w") as output, (directory / "stderr.log").open("w") as errors:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                   env=environment(runtime, home), start_new_session=True)
        write(directory / "process.json", {"pid": process.pid, "started_at": started,
                                           "model": role["model"], "effort": role["effort"],
                                           "thread_id": thread, "scope": unit + ".scope"})
        try:
            process.communicate(prompt.encode(), timeout=timeout)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["systemctl", "--user", "stop", unit + ".scope"], check=True, timeout=15)
            exit_code = process.wait(timeout=30)
            error = f"timeout after {timeout}s"
    usage = cli_usage(events)
    snapshot = thread_usage(home, usage["thread_id"] or thread) if usage["thread_id"] or thread else {}
    for key, value in usage["usage"].items():
        if value is None and key in snapshot:
            usage["usage"][key] = snapshot[key]
    raw_usage = dict(usage["usage"])
    if thread:
        if previous_usage is None:
            raise ValueError("Resume usage requires the previous cumulative snapshot")
        for key, value in raw_usage.items():
            prior = previous_usage.get(key)
            usage["usage"][key] = value-prior if value is not None and prior is not None else None
            if usage["usage"][key] is not None and usage["usage"][key] < 0:
                raise ValueError("Token counter decreased; cannot infer per-call usage")
    result = {**usage, "role": role, "started_at": started, "seconds": time.time()-started,
              "raw_cumulative_usage": raw_usage, "exit_code": exit_code, "error": error,
              "deadline_reached": error is not None,
              "usage_measurement": "last_reported_snapshot_lower_bound" if error else "completed_turn",
              "prompt_sha256": digest(directory / "prompt.txt")}
    write(directory / "usage.json", result)
    if error or exit_code or usage["completed_turns"] != 1:
        raise RuntimeError(f"Agent call failed; see {directory}")
    return result


def role_home(run, role, runtime):
    home = run / "homes" / role
    home.mkdir(parents=True, exist_ok=True)
    auth = home / "auth.json"
    if not auth.exists():
        shutil.copyfile(Path(runtime["auth_home"]) / "auth.json", auth)
        auth.chmod(0o600)
    return home


def initialize_run(root, run, spec, runtime):
    shared = root / "shared" / spec["scene"]
    source = shared / "project"
    project = run / "project"
    if project.exists():
        raise ValueError("A new trial cannot reuse an existing project")
    project.mkdir()
    for name in ("observations",):
        shutil.copytree(source / name, project / name)
    for name in ("split_manifest.json", "calibration_audit.json", "training_contact_sheet.jpg"):
        shutil.copyfile(source / name, project / name)
    protocol = read(root / "protocol.json")
    write(project / "cameras.json", camera_manifest(read(shared / "source/transforms.json"),
                                                   protocol["training_frames"]))
    views = read(shared / "views.json")
    if spec["condition"] != "single_basic":
        for name in ("dense", "assets"):
            shutil.copytree(source / name, project / name)
        shutil.copyfile(source / "reconstruction_report.json", project / "reconstruction_report.json")
        scene = read(source / "world/scene.json")
    else:
        scene = {"schema_version": 1, "revision": 1, "scene_id": spec["scene"], "name": spec["scene"],
                 "units": "arbitrary", "up_axis": "Z", "provenance": {"kind": "reconstruction",
                 "description": "RGB/camera-only schema stub; replace the tiny off-scene example object.",
                 "sources": [{"path":"observations/manifest.json", "description":"Frozen input images"}]},
                 "objects": [{"id":"schema_example", "name":"Schema example; remove", "parent_id":None,
                 "position":[10000,10000,10000], "rotation":[0,0,0], "visual":[{"type":"box","size":[.001,.001,.001]}],
                 "collision":[{"type":"box","size":[.001,.001,.001]}],
                 "material":{"base_color":[.5,.5,.5,1],"roughness":.8,"metallic":0},
                 "physics":{"body_type":"static","mass":0,"friction":.5},
                 "evidence":{"geometry":"estimated","physics":"estimated","notes":"Schema only"}}]}
    scene["views"] = views
    if protocol.get("appearance_regime"):
        scene["color_management"] = {"view_transform": "Standard", "exposure": 0}
    write(project / "world/scene.json", scene)
    write(project / "experiment.json", spec)
    write(project / "edit_task_targets.json", read(root / "protocol.json")["edit_targets"][spec["scene"]])
    write(project / "tool_runtime.json", {**{k: runtime[k] for k in
          ("da3_code","geometry_image","segmentation_image","sam_code","sam_weights","blender")},
          "docker_gpus": runtime.get("docker_gpus", "all"),
          "tool_timeout_seconds": runtime.get("tool_timeout_seconds", 3600),
          "build_glb_limit_bytes": protocol.get("build_glb_limit_bytes")})
    if spec["condition"] != "single_basic":
        write(project / "plan_schema.json", PLAN_SCHEMA)
    packet = f"""# Experiment task packet
Only read this project and generic implementation at {REPO}. Do not read experiment siblings, old models, source dataset directories, private evaluation images or other agents' sessions. No web/asset download or additional agent delegation. Physics is deferred.
Input: observations/manifest.json, images, cameras.json (all training and validation poses), training_contact_sheet.jpg. Preserve these inputs and world/scene.json. Six calibrated validation cameras in world/scene.json must remain unchanged in every candidate. The schema-only object (if present) is not scene geometry. Separate visible objects and give stable IDs. Unseen geometry must be marked estimated.
Scene JSON format: follow world/scene.json and {REPO}/backend/world_model.py. Save candidate.json, edit_targets.json, scripts and assets early inside each checkpoint's model directory. Finish your turn within its time budget. The controller builds and validates your saved candidate after the turn; no self-written receipt is required. Optional preview: python3 {Path(__file__).with_name('tools.py')} --project {project} build --scene <candidate.json>. Preserve prior checkpoints, inputs, world coordinates and units. Maximum 256 objects and 1,000,000 visual triangles. Do not modify implementation or experiment records.
Common editing tasks are listed in edit_task_targets.json. Next to each candidate write edit_targets.json mapping each listed key to the corresponding scene object ID, or null if the target is missing or unresolved. This declaration will be independently inspected; naming an object is not evidence of correct ownership.
Specialist tools are enabled only for tool conditions: use tools.py segment --plan <plan.json> --output <new-dir>; geometry --plan <plan.json> --segments <segments.json> --output <new-dir>; depth --receipt <build_receipt.json>; triangulate --receipt <receipt> --landmarks <landmarks.json>. A self-authored plan must follow plan_schema.json. Do not run specialist tools in single_basic. Tools do not certify reconstruction correctness.
"""
    if spec["condition"] != "harness_tools":
        packet = f"""根据原始图像独立重建并修订场景，物体可独立编辑，保存建模源码。物理暂不处理，无须先提交计划或物体清单。
输入见observations/manifest.json、cameras.json和training_contact_sheet.jpg；保留原始输入、坐标系和world/scene.json中的六台相机。仅查看本项目及{REPO}通用代码，不读其他实验、旧模型或私有评测，不下载外部素材或调用额外Agent。
输出指定目录的candidate.json与edit_targets.json（对应edit_task_targets.json；找不到的目标填null）；格式见world/scene.json及{REPO}/backend/world_model.py。上限256对象、100万三角形；未知几何标estimated。控制器负责最终构建，按需做普通Blender预览。工具说明见TOOLS.md。
不提供外部审核，不调用Harness审核材料生成器、原图/渲染对照面板、叠图或RGB差异图。
"""
    packet += appearance_instruction(protocol.get("appearance_regime")) + "\n"
    if protocol.get("appearance_regime"):
        packet += f"灯光和材质字段示例按需查阅{REPO}/experiments/reconstruction/APPEARANCE.md。\n"
    (project / "AGENTS.md").write_text(packet)
    (project / "TOOLS.md").write_text(
        f"场景预览：python3 {Path(__file__).with_name('tools.py')} --project {project} build --scene <candidate.json>\n"
        + ("本组不使用专业分割、深度或重建工具。\n" if spec["condition"] == "single_basic" else
           f"专业工具：python3 {Path(__file__).with_name('tools.py')} --help。可按需调用segment、geometry、depth、triangulate；"
           "segment/geometry需要的请求格式见plan_schema.json。这里的工具请求不要求独立计划Agent。\n"))
    validate_scene(scene, project)
    protected = [p for p in (project / "observations").rglob("*") if p.is_file()]
    if spec["condition"] != "single_basic":
        protected += [p for name in ("dense", "assets") for p in (project / name).rglob("*") if p.is_file()]
        protected.append(project / "reconstruction_report.json")
    protected += [project / name for name in ("split_manifest.json", "calibration_audit.json",
                  "training_contact_sheet.jpg", "cameras.json", "world/scene.json", "experiment.json", "AGENTS.md",
                  "edit_task_targets.json", "tool_runtime.json", "TOOLS.md")]
    write(run / "input_hashes.json", {str(p.relative_to(project)): digest(p) for p in protected})
    return project


def planning(project, run, runtime, protocol):
    home = role_home(run, "planner", runtime)
    previous = {k: os.environ.get(k) for k in ("HTTP_PROXY","HTTPS_PROXY","NO_PROXY")}
    os.environ.update({k: environment(runtime, home)[k] for k in previous})
    role = {**protocol["planner"], "codex": runtime["codex"], "home": str(home)}
    started = time.time()
    planner_error = None
    try:
        relative, plan = run_planner(project, project / "evidence/plan", {"planner":role,
                           "planner_timeout":protocol["planner_timeout_seconds"]})
    except Exception as error:
        planner_error = str(error)
        raise
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        events = project / "evidence/plan/events.jsonl"
        if events.exists():
            usage = cli_usage(events)
            snapshot = thread_usage(home, usage["thread_id"]) if usage["thread_id"] else {}
            for key, value in usage["usage"].items():
                if value is None and key in snapshot:
                    usage["usage"][key] = snapshot[key]
            write(run / "planner_usage.json", {**usage,"seconds":time.time()-started,
                  "role":protocol["planner"],"error":planner_error,
                  "usage_measurement":"completed_turn" if usage["completed_turns"] else "last_reported_snapshot_lower_bound"})
    toolbox = Path(__file__).with_name("tools.py")
    for op, extra in (("segment",[]), ("geometry",["--segments","evidence/segments/segments.json"])):
        output = "evidence/segments" if op == "segment" else "evidence/geometry"
        subprocess.run([sys.executable,str(toolbox),"--project",str(project),op,"--plan",relative,
                        "--output",output,*extra],check=True,timeout=runtime.get("tool_timeout_seconds", 3600))
    return relative


def freeze_checkpoint(project, directory, candidate, receipt):
    directory.mkdir(parents=True, exist_ok=True)
    scene = validate_scene(read(project / candidate), project)
    validate_fixed_views(project, scene)
    build = read(project / receipt)
    frozen = directory / "sources"
    for name in source_paths(scene):
        destination = frozen / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / name, destination)
    for path in (project / candidate).parent.rglob("*"):
        if path.is_file() and (path.suffix in {".py", ".sh"} or path.name == "modeling_report.json"):
            destination = frozen / path.relative_to(project)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    for key, suffix in (("blend_path","blend"),("glb_path","glb")):
        shutil.copyfile(build[key], directory / f"scene.{suffix}")
    write(directory / "scene.json", scene)
    write(directory / "receipt.json", build)
    shutil.copyfile((project / candidate).parent / "edit_targets.json", directory / "edit_targets.json")
    files = {str(f.relative_to(directory)): digest(f) for f in directory.rglob("*") if f.is_file()}
    write(directory / "frozen.json", {"scene_hash":build["scene_hash"],"files":files})
    return scene, build


def next_attempt(parent, name):
    path, number = parent / name, 1
    while path.exists():
        number += 1
        path = parent / f"{name}_attempt{number}"
    return path


def feedback(project, run, runtime, protocol, checkpoint, candidate, receipt, plan, previous):
    """Only the full harness gets generated review evidence and a reviewer."""
    if not plan:
        return None
    number = checkpoint + 1
    completed = sorted((project / "reviews").glob(f"R{number}*/review.json"), key=lambda p: p.stat().st_mtime)
    if completed:
        path = completed[-1]
        manifest = read(path.parent / "candidate.json")
        verify_files(project, manifest["files"])
        validate_review(manifest, read(path), read(project / previous) if previous else None)
        return str(path.relative_to(project))
    # Old M0 geometry stays frozen. Add omitted input lineage to a separate
    # review build rather than editing the saved modeling candidate.
    original = read(project / candidate)
    bound_path = Path("review_bindings") / f"M{checkpoint}" / "candidate.json"
    write(project / bound_path, original)
    bound = bind_candidate_sources(project, bound_path)
    ready = sorted((project/"reviews").glob(f"R{number}*/candidate.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    cached = next((p for p in ready if read(p)["scene_hash"] == scene_hash(bound)), None)
    if cached:
        frozen, manifest = cached.parent, read(cached)
        verify_files(project, manifest["files"])
    else:
        if scene_hash(bound) != scene_hash(original):
            build = build_world(project, blender=runtime["blender"], scene=bound, render=True, timeout=900,
                                max_glb_bytes=protocol.get("build_glb_limit_bytes"))
            candidate, receipt = str(bound_path), str(bound_path.parent / "build_receipt.json")
            write(project / receipt, build)
            write(project / bound_path.parent / "migration.json", {
                "original_scene_hash": scene_hash(original), "review_scene_hash": scene_hash(bound),
                "geometry_and_views_unchanged": original["objects"] == bound["objects"] and original["views"] == bound["views"]})
        frozen = next_attempt(project / "reviews", f"R{number}")
        manifest = freeze_candidate(project, candidate, receipt, frozen, blender=runtime["blender"],
            previous_review=project/previous if previous else None, plan_path=plan,
            segments_path="evidence/segments/segments.json", geometry_path="evidence/geometry/geometry.json",
            modeling_report_path=f"models/M{checkpoint}/modeling_report.json")
    inspection, edit = read(frozen/"inspection.json"), read(frozen/"editability.json")
    images = (inspection["inspection_paths"] + inspection.get("overlay_paths", []) +
              inspection["depth"].get("inspection_paths", []) + manifest.get("source_inspection_paths", []) +
              edit.get("inspection_paths", []))
    call = next_attempt(run / "calls", f"review_R{number}")
    agent(project, call, runtime, protocol["reviewer"], role_home(run, "reviewer", runtime),
          review_prompt(project, frozen, project/previous if previous else None,
                        appearance_regime=protocol.get("appearance_regime")),
          images=[project/str(p) for p in images], schema=review_schema(manifest),
          timeout=protocol["review_timeout_seconds"])
    report = read(call / "response.json")
    verify_files(project, manifest["files"])
    validate_review(manifest, report, read(project/previous) if previous else None)
    write(frozen/"review.json", report)
    return str((frozen/"review.json").relative_to(project))


def run_trial(root, spec, runtime, protocol):
    identifier = f"{spec['scene']}__{spec['condition']}__{spec['model']}__{spec['effort']}"
    run = root / "runs" / identifier
    seeded = (run / "resume.json").exists()
    if seeded:
        seed = read(run / "resume.json")
        state = read(run / "status.json")
    else:
        run.mkdir(parents=True, exist_ok=False)
        write(run / "spec.json", spec)
        state = {"id":identifier,"spec":spec,"stage":"preparing","started_at":time.time(),"checkpoints":[]}
    def update(stage, **fields):
        state.update(stage=stage,updated_at=time.time(),**fields)
        write(run / "status.json", state)
    try:
        project = (run / "project").resolve() if seeded else initialize_run(root, run, spec, runtime)
        plan = seed.get("plan") if seeded else None
        if spec["condition"] == "harness_tools" and not plan:
            update("planning")
            plan = planning(project, run, runtime, protocol)
        thread = seed.get("thread_id") if seeded else None
        previous_usage = seed.get("raw_cumulative_usage") if seeded else None
        if seeded:
            handoff = model_handoff(run)
            if handoff["thread_id"]:
                thread, previous_usage = handoff["thread_id"], handoff["raw_cumulative_usage"]
        previous, start = None, len(state["checkpoints"])
        for number in range(start):
            verify_files(run/f"checkpoints/M{number}", read(run/f"checkpoints/M{number}/frozen.json")["files"])
            update("preparing_feedback", checkpoint=number)
            previous = feedback(project, run, runtime, protocol, number,
                                state["checkpoints"][number]["candidate"], state["checkpoints"][number]["receipt"], plan, previous)
        for checkpoint in range(start, protocol["modeling_passes"]):
            model_dir = project / "models" / f"M{checkpoint}"
            model_dir.mkdir(parents=True, exist_ok=seeded)
            if plan:
                write(model_dir / "modeling_report_schema.json", MODEL_REPORT_SCHEMA)
            candidate = str((model_dir / "candidate.json").relative_to(project))
            receipt = str((model_dir / "build_receipt.json").relative_to(project))
            prompt = protocol["input_prompt"] if checkpoint == 0 else protocol["repair_prompt"]
            budget = protocol["modeling_timeout_seconds"][checkpoint]
            prompt += f"\n输出 {candidate}（revision=2）及同目录 edit_targets.json；基本说明见 AGENTS.md。"
            prompt += (f"\n本次建模预算 {budget} 秒，须在预算内保存候选并结束本次调用。" if budget is not None else
                       "\n本次返修不设时间上限，完成本轮修改、保存候选和报告后结束调用。")
            prompt += "最终构建与程序校验由控制器完成。"
            if plan:
                prompt += (f"\n初始化计划 {plan}；分割 evidence/segments/segments.json；几何 evidence/geometry/geometry.json。"
                           f"逐项处理物体和未解释区域；输出同目录 modeling_report.json，遵循 modeling_report_schema.json。"
                           "\n建模阶段完成几何、空间关系、物体拆分及外观修改，按需检查局部。"
                           "整套逐物体移动、隐藏与保存重开检查由审核阶段统一执行，勿重复执行整套验收。")
            if previous and plan:
                prompt += f"\n本次审核意见：{previous}。保留旧问题ID并按对应原图修复。"
            elif previous:
                prompt += f"\n自检材料：{previous}。打开其中原图、纯渲染和叠图，对照后自行修订。"
            if checkpoint:
                prompt += f"\n前一候选 models/M{checkpoint-1}/candidate.json；依据原始输入自行修订" + ("，并处理独立审核意见。" if plan else "。")
            if seeded and checkpoint == start and plan:
                prompt += "\n这是中断任务的续接，保留本轮已保存的修改并完成提交，不从头重建场景。"
            update("modeling",checkpoint=checkpoint)
            submission = seed.get("saved_submission") if seeded else None
            if submission and submission["checkpoint"] == checkpoint:
                if digest(project/candidate) != submission["candidate_sha256"]:
                    raise ValueError("Saved submission changed before continuation")
                usage = read(run/submission["call"]/"usage.json")
                if usage.get("error") or usage["exit_code"] != 0 or usage["completed_turns"] != 1:
                    raise ValueError("Saved submission must come from a completed modeling turn")
            else:
                usage = agent(project,next_attempt(run/"calls", f"model_M{checkpoint}"),runtime,
                              {"model":spec["model"],"effort":spec["effort"]},role_home(run,"modeler",runtime),
                              prompt,images=[project/"training_contact_sheet.jpg"],thread=thread,
                              previous_usage=previous_usage,
                              timeout=budget)
            verify_files(project, read(run / "input_hashes.json"))
            thread = usage["thread_id"] or thread
            previous_usage = usage["raw_cumulative_usage"]
            scene = bind_candidate_sources(project, candidate)
            validate_fixed_views(project, scene)
            build = build_world(project, blender=runtime["blender"], scene=scene, render=True, timeout=900,
                                max_glb_bytes=protocol.get("build_glb_limit_bytes"))
            write(project/receipt, build)
            scene, build = freeze_checkpoint(project,run/f"checkpoints/M{checkpoint}",candidate,receipt)
            state["checkpoints"].append({"name":f"M{checkpoint}","candidate":candidate,
                                          "receipt":receipt,"objects":len(scene["objects"]),"usage":usage["usage"]})
            update("checkpoint_frozen",checkpoint=checkpoint,thread_id=thread)
            if checkpoint == 2:
                continue
            if plan:
                update("reviewing",checkpoint=checkpoint)
            previous = feedback(project, run, runtime, protocol, checkpoint, candidate, receipt, plan, previous)
        update("modeled",seconds=time.time()-state["started_at"],final_review="not_run_by_protocol")
    except Exception as error:
        update("failed",error=str(error),seconds=time.time()-state["started_at"])
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--runtime",type=Path,required=True)
    parser.add_argument("--scene")
    parser.add_argument("--model")
    parser.add_argument("--condition")
    parser.add_argument("--effort")
    args = parser.parse_args()
    protocol,runtime = read(args.root/"protocol.json"),read(args.runtime)
    for name in protocol["scenes"]:
        if args.scene and args.scene!=name:
            continue
        for row in matrix(protocol):
            if (args.root / "launch_hold.json").exists():
                return
            if any(getattr(args,k) and getattr(args,k)!=row[k] for k in ("model","condition","effort")):
                continue
            spec={"scene":name,**row,"protocol_sha256":digest(args.root/"protocol.json")}
            if protocol.get("appearance_regime"):
                spec["appearance_regime"] = protocol["appearance_regime"]
            identifier=f"{name}__{row['condition']}__{row['model']}__{row['effort']}"
            existing = args.root/"runs"/identifier
            if existing.exists() and not ((existing/"resume.json").exists() and read(existing/"status.json")["stage"] == "queued"):
                continue
            try:
                run_trial(args.root,spec,runtime,protocol)
            except Exception as error:
                print(f"{identifier}: {error}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
