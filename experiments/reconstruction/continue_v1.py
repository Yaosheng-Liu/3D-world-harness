"""Prepare a separate continuation cohort; preserve v1 checkpoints and records."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import time

from common import cli_usage, digest, matrix, read, thread_usage, write
from run import initialize_run
from reconstruction_review import verify_files
from world_model import validate_scene


def clean_single_project(source, project):
    """Copy construction code/assets, excluding review outputs and old repairs."""
    def exclude(directory, names):
        if "assets" in Path(directory).parts:
            return []
        return [name for name in names if name in {"diagnostics", "evaluation", "self_inspection.json", "build_receipt.json"}
                or Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
    shutil.copytree(source / "models/M0", project / "models/M0", ignore=exclude)
    original = read(source / "models/M0/candidate.json")
    dependencies = {shape["path"] for obj in original["objects"] for shape in obj["visual"] if shape["type"] == "mesh"}
    dependencies.update(obj["material"]["base_color_texture"] for obj in original["objects"] if "base_color_texture" in obj["material"])
    for name in dependencies:
        destination = project / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    for path in (project / "models/M0").rglob("*"):
        if path.is_file() and path.name != "candidate.json" and path.suffix in {".py", ".json", ".sh", ".cpp"}:
            path.write_text(path.read_text().replace(str(source), str(project)))
    path = project / "models/M0/candidate.json"
    scene = read(path)
    scene["provenance"]["sources"] = [row for row in scene["provenance"]["sources"]
                                        if (project / row["path"]).is_file()]
    write(path, scene)
    validate_scene(scene, project)


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


def copy_calls(source, run, single):
    for call in source.glob("calls/*"):
        if single and call.name != "model_M0":
            continue
        if (call/"usage.json").exists():
            data = read(call/"usage.json")
        elif (call/"process.json").exists():
            process = read(call/"process.json")
            data = cli_usage(call/"events.jsonl")
            data.update(started_at=process["started_at"], seconds=read(call/"interruption.json")["at"]-process["started_at"],
                        raw_cumulative_usage=model_handoff(source)["raw_cumulative_usage"], error="Interrupted for runner repair")
        else:
            continue
        write(run/"source_calls"/call.name/"usage.json", data)


def prepare(source, root):
    root.mkdir(parents=True, exist_ok=False)
    protocol = read(source/"protocol.json")
    protocol.update(id=root.name, modeling_timeout_seconds=[3600, None, None], halt_after_consecutive_failures=2,
                    submission=read(Path(__file__).with_name("protocol.json"))["submission"],
                    single_agent_feedback=read(Path(__file__).with_name("protocol.json"))["single_agent_feedback"],
                    continuation_source=str(source))
    write(root/"protocol.json", protocol)
    shutil.copyfile(source/"runtime.json", root/"runtime.json")
    runtime = read(root/"runtime.json")
    for name in ("shared", "evaluation_private"):
        (root/name).symlink_to(source/name, target_is_directory=True)
    for name in ("inputs.json", "initialization_summary.json", "v1_resume_integrity_audit.json"):
        shutil.copyfile(source/name, root/name)
    manifest, excluded = [], []
    for scene in protocol["scenes"]:
        for row in matrix(protocol):
            identifier = f"{scene}__{row['condition']}__{row['model']}__{row['effort']}"
            old = source/"runs"/identifier
            frozen = old/"checkpoints/M0/frozen.json"
            single = row["condition"] != "harness_tools"
            partial_harness = not single and (old/"project/evidence/plan/plan.json").exists()
            if not frozen.exists() and not partial_harness:
                manifest.append({"trial": identifier, "origin": "fresh_missing_initial", "prior_trial": str(old)})
                continue
            run = root/"runs"/identifier
            run.mkdir(parents=True)
            spec = {"scene": scene, **row, "protocol_sha256": digest(root/"protocol.json"),
                    "initial_origin": "inherited_v1" if frozen.exists() else "continued_v1_initial"}
            write(run/"spec.json", spec)
            if single:
                project = initialize_run(root, run, spec, runtime)
                clean_single_project(old/"project", project)
                seed = {"thread_id": None, "raw_cumulative_usage": None, "plan": None,
                        "repair_context": "clean; old audit-exposed repair excluded"}
                initial_usage = read(old/"calls/model_M0/usage.json")
                write(run/"initial_modeler_usage.json", initial_usage["usage"])
                for call in old.glob("calls/model_M[12]*/usage.json"):
                    record = read(call)
                    snapshot = thread_usage(old/"homes/modeler", record["thread_id"])
                    excluded.append({"trial": identifier, "source_call": str(call), "seconds": record["seconds"],
                        "usage_since_M0_lower_bound": {key: value-initial_usage["usage"][key]
                            if initial_usage["usage"].get(key) is not None else None for key,value in snapshot.items()},
                        "reason": "Old repair had access to system review comparisons; excluded from clean single-agent condition"})
            else:
                (run/"project").symlink_to(old/"project", target_is_directory=True)
                for role in ("modeler", "planner", "reviewer"):
                    home = old/"homes"/role
                    if home.exists():
                        (run/"homes").mkdir(exist_ok=True)
                        (run/"homes"/role).symlink_to(home, target_is_directory=True)
                seed = {**model_handoff(old), "plan": "evidence/plan/plan.json"}
                shutil.copyfile(old/"input_hashes.json", run/"input_hashes.json")
            checkpoints = []
            if frozen.exists():
                verify_files(frozen.parent, read(frozen)["files"])
                shutil.copytree(frozen.parent, run/"checkpoints/M0", copy_function=os.link)
                original = read(frozen.parent/"scene.json")
                current = read(run/"project/models/M0/candidate.json")
                for key in ("objects", "views", "units", "up_axis"):
                    if current[key] != original[key]:
                        raise ValueError(f"Initial scene changed while preparing {identifier}: {key}")
                checkpoints = read(old/"status.json")["checkpoints"][:1]
                evaluated = source/"evaluation_results"/identifier/"M0"
                if (evaluated/"complete.json").exists():
                    shutil.copytree(evaluated, root/"evaluation_results"/identifier/"M0", copy_function=os.link)
            if (old/"planner_usage.json").exists():
                shutil.copyfile(old/"planner_usage.json", run/"planner_usage.json")
            copy_calls(old, run, single)
            seed.update(source_trial=str(old), source_status=read(old/"status.json"), prepared_at=time.time())
            write(run/"resume.json", seed)
            write(run/"status.json", {"id":identifier, "spec":spec, "stage":"queued", "started_at":time.time(),
                                      "checkpoints":checkpoints, "source_trial":str(old)})
            manifest.append({"trial":identifier, "origin":spec["initial_origin"], "source_trial":str(old),
                             "new_repair_context":single, "frozen_M0_unchanged":bool(checkpoints)})
    write(root/"continuation_manifest.json", manifest)
    write(root/"excluded_feedback_attempts.json", excluded)
    write(root/"registered_protocol.json", {"registered_at":time.time(), "protocol_sha256":digest(root/"protocol.json"),
          "source_batch":str(source), "inherited_initials":sum(m["origin"]=="inherited_v1" for m in manifest),
          "original_initial_limits_seconds":read(source/"protocol.json")["modeling_timeout_seconds"],
          "note":"Retrospective continuation, not a new equal-budget benchmark. Initial budget history is disclosed; contaminated single-agent repair attempts are excluded from condition costs and retained in the source batch."})
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source.resolve(), args.root.resolve())
