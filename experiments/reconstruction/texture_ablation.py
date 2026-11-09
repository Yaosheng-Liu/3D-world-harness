"""Render-only appearance ablation; never exposes private views to agents."""
import argparse
from concurrent.futures import as_completed, ThreadPoolExecutor
import csv
from pathlib import Path
import time

from common import REPO, command, digest, docker, read, write


def evaluate(root, output, trial, checkpoint, runtime):
    destination = output / trial.name / checkpoint
    frozen = trial / "checkpoints" / checkpoint
    manifest = read(frozen / "frozen.json")
    for name in ("scene.blend", "scene.json"):
        if digest(frozen / name) != manifest["files"][name]:
            raise ValueError("Frozen input changed: " + str(frozen / name))
    original = read(root / "evaluation_results" / trial.name / checkpoint / "complete.json")
    if original["scene_hash"] != manifest["scene_hash"]:
        raise ValueError("Existing geometry metrics belong to another scene")
    complete = destination / "complete.json"
    if complete.exists():
        result = read(complete)
        if result["scene_hash"] != manifest["scene_hash"]:
            raise ValueError("Completed ablation belongs to another scene")
        return {**result, "geometry_reused": original["geometry"]["macro_average_metrics"]}
    started = time.time()
    job = read(root / "evaluation_results" / trial.name / checkpoint / "job.json")
    job["output"], job["blend"] = str(destination), str(frozen / "scene.blend")
    write(destination / "job.json", job)
    command([runtime["blender"], "--background", "--factory-startup", "--python-exit-code", "1",
             "--python", str(Path(__file__).with_name("texture_ablation_blender.py")),
             "--", str(destination / "job.json")], destination / "blender.log", timeout=7200)
    spec, scene = read(trial / "spec.json"), read(frozen / "scene.json")
    private = root / "evaluation_private" / spec["scene"]
    metrics = {}
    for variant in ("textured", "clay"):
        folder = destination / variant
        argv = docker(runtime, [(REPO, "ro"), (private, "ro"), (destination, "rw")],
                      Path(__file__).with_name("image_metrics.py"),
                      ["--references", private / "render_references", "--renders", folder / "renders",
                       "--output", folder / "image_metrics.json"])
        position = argv.index("--entrypoint")
        argv[position:position] = ["-v", f"{runtime['evaluation_packages']}:/evaluation-packages:ro",
                                  "-v", f"{runtime['torch_cache']}:/torch-cache:ro", "-e",
                                  "PYTHONPATH=/evaluation-packages", "-e", "TORCH_HOME=/torch-cache"]
        command(argv, folder / "image.log")
        metrics[variant] = read(folder / "image_metrics.json")
    for name in ("scene.blend", "scene.json"):
        if digest(frozen / name) != manifest["files"][name]:
            raise ValueError("Ablation modified frozen input")
    result = {"trial": trial.name, "checkpoint": checkpoint, "spec": spec,
              "scene_hash": manifest["scene_hash"], "input_blend_sha256": manifest["files"]["scene.blend"],
              "objects": len(scene["objects"]),
              "textured_objects": sum("base_color_texture" in o["material"] for o in scene["objects"]),
              "geometry_reused": original["geometry"]["macro_average_metrics"],
              "metrics": metrics, "seconds": time.time() - started}
    write(complete, result)
    print("COMPLETED", trial.name, checkpoint, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoint", choices=("M0", "M1", "M2"), default="M2")
    parser.add_argument("--trial")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    args.root, args.output = args.root.resolve(), args.output.resolve()
    protocol = {"source_root": str(args.root), "checkpoint": args.checkpoint,
                "variants": ["textured", "clay"], "illumination": "camera AREA 70W, size 2; neutral world .1",
                "material": "Principled, roughness .8, metallic 0; clay base color .5 linear RGB",
                "color_management": "Standard, None, exposure 0, gamma 1",
                "interpretation": "Existing captured RGB versus uniform gray under diagnostic illumination. "
                                  "Textured mode preserves existing maps or constant colors; missing maps are not generated. "
                                  "Gray RGB similarity is not a geometry metric. No relighting accuracy claim.",
                "renderer_sha256": digest(Path(__file__).with_name("texture_ablation_blender.py"))}
    path = args.output / "protocol.json"
    if path.exists() and read(path) != protocol:
        raise ValueError("Output already contains another ablation protocol")
    write(path, protocol)
    runtime = read(args.runtime)
    trials = sorted((args.root / "runs").glob(args.trial or "*"))
    if not trials:
        raise ValueError("No trials selected")
    (args.output / "completion.json").unlink(missing_ok=True)
    results = []
    write(args.output / "progress.json", {"expected_trials": len(trials), "completed_trials": 0})
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate, args.root, args.output, trial, args.checkpoint, runtime) for trial in trials]
        for future in as_completed(futures):
            results.append(future.result())
            write(args.output / "progress.json", {"expected_trials": len(trials),
                  "completed_trials": len(results), "updated_at": time.time()})
    results.sort(key=lambda result: result["trial"])
    rows = [{"trial": r["trial"], **{key: r["spec"][key] for key in ("scene", "condition", "model", "effort")},
             "checkpoint": r["checkpoint"],
             "objects": r["objects"], "textured_objects": r["textured_objects"],
             **r["geometry_reused"],
             "variant": variant, **metric["mean"]}
            for r in results for variant, metric in r["metrics"].items()]
    with (args.output / "results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write(args.output / "results.json", rows)
    write(args.output / "completion.json", {"trials": len(results), "variants": len(rows),
          "renders": sum(len(m["views"]) for r in results for m in r["metrics"].values()),
          "completed_at": time.time()})


if __name__ == "__main__":
    main()
