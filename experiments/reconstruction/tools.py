"""The same build interface for all arms; specialist tools only for tool arms."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from common import REPO, bind_candidate_sources, command, docker, read, write

sys.path[:0] = [str(REPO / "backend"), str(REPO / "scripts")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("operation", choices=("build", "segment", "geometry", "depth", "triangulate"))
    parser.add_argument("--scene", type=Path)
    parser.add_argument("--plan")
    parser.add_argument("--segments")
    parser.add_argument("--output")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--landmarks", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    spec = read(project / "experiment.json")
    runtime = read(project / "tool_runtime.json")
    if args.operation != "build" and spec["condition"] == "single_basic":
        raise ValueError("Specialist tools are excluded from this condition")
    started = time.time()
    if args.operation == "build":
        from world_build import build_world
        result = build_world(project, blender=runtime["blender"], scene=bind_candidate_sources(project, args.scene), render=True, timeout=900,
                             max_glb_bytes=runtime.get("build_glb_limit_bytes"))
        output = project / args.scene.parent / "build_receipt.json"
        write(output, result)
        result = {"receipt_path": str(output), "blend_path": result["blend_path"],
                  "render_paths": result["render_paths"], "object_ids": result["object_ids"]}
    elif args.operation in {"segment", "geometry"}:
        if not args.plan or not args.output:
            raise ValueError("--plan and --output are required")
        destination = project / args.output
        destination.parent.mkdir(parents=True, exist_ok=True)
        mounts = [(REPO, "ro"), (project, "rw"),
                  (Path(read(project / "split_manifest.json")["source_directory"]), "ro")]
        if args.operation == "segment":
            argv = ["docker", "run", "--rm", "--network", "none", "--gpus", runtime.get("docker_gpus", "all"),
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "-v", f"{runtime['sam_code']}:/workspace/projects/sam3:ro",
                    "-v", f"{runtime['sam_weights']}:/workspace/models/sam3:ro"]
            for path, mode in mounts:
                argv += ["-v", f"{path}:{path}:{mode}"]
            argv += ["--entrypoint", "python", runtime["segmentation_image"], str(REPO / "scripts/sam3_segment.py"),
                     "--project", str(project), "--plan", args.plan, "--output", args.output,
                     "--checkpoint", "/workspace/models/sam3/sam3.pt"]
            if runtime.get("gpu_lock"):
                argv = ["flock", runtime["gpu_lock"], *argv]
            filename = "segments.json"
        else:
            if not args.segments:
                raise ValueError("--segments required")
            argv = docker(runtime, mounts, REPO / "scripts/reconstruct_objects.py",
                          ["--project", project, "--plan", args.plan, "--segments", args.segments,
                           "--output", args.output])
            filename = "geometry.json"
        command(argv, destination.with_suffix(".log"), timeout=runtime.get("tool_timeout_seconds", 3600))
        result = {"report_path": str(destination / filename)}
    elif args.operation == "depth":
        from world_depth import write_depth_inspection
        receipt = read(project / args.receipt)
        scene = read(Path(receipt["build_dir"]) / "job.json")["scene"]
        output = project / "diagnostics" / f"depth_{time.time_ns()}"
        result = write_depth_inspection(project, scene, receipt, output, blender=runtime["blender"])
    else:
        from world_landmarks import inspect_landmarks
        result = inspect_landmarks(project, args.receipt, args.landmarks)
    with (project / "tool_calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"operation": args.operation, "started_at": started,
                                 "seconds": time.time()-started}) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
