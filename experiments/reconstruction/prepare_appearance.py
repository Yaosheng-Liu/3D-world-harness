"""Two fresh ultra-only cohorts sharing the same raw observation cache."""
import argparse
import os
from pathlib import Path
import shutil
import time

from common import REPO, digest, matrix, read, write


def prepare(source, root):
    source, root = source.resolve(), root.resolve()
    original = read(source / "protocol.json")
    root.mkdir(parents=True, exist_ok=False)
    for regime in ("captured", "intrinsic"):
        destination = root / regime
        destination.mkdir()
        protocol = {**original, "id": f"{root.name}_{regime}", "appearance_regime": regime,
                    "main_effort": "ultra", "ablation_effort": None,
                    "conditions": ["harness_tools", "single_basic", "single_tools"],
                    "planner": {"model": "gpt-6-astra", "effort": "ultra"},
                    "reviewer": {"model": "gpt-6.1-sol", "effort": "ultra"},
                    "single_agent_feedback": "Original observations, prior scene and short revision prompt only. "
                                             "No independent planner, inventory checklist or external review evidence.",
                    "initialization_cost": "Reuse identical raw-training DA3/TSDF cache; record original setup cost "
                                           "separately. All model, plan and review conversations start fresh.",
                    "submission_checks": "Basic engineering validity plus explicit appearance-mode fields. "
                                         "Texture provenance and shadow separation require visual/source inspection.",
                    "color_management": {"view_transform": "Standard", "exposure": 0},
                    "code_revision": "See preparation.json; do not reuse prior candidates or agent contexts."}
        protocol.pop("continuation_source", None)
        write(destination / "protocol.json", protocol)
        write(destination / "registered_protocol.json", protocol)
        shutil.copyfile(source / "runtime.json", destination / "runtime.json")
        (destination / "shared").symlink_to((source / "shared").resolve(), target_is_directory=True)
        # Input files are immutable; generated resized references get a new directory.
        shutil.copytree(source / "evaluation_private", destination / "evaluation_private",
                        copy_function=os.link, ignore=shutil.ignore_patterns("render_references"))
        for name in ("inputs.json", "initialization_summary.json"):
            shutil.copyfile(source / name, destination / name)
        (destination / "runs").mkdir()
        write(destination / "scheduler.json", {"workers_per_model": 2, "before_gate_trials": []})
        write(destination / "preparation.json", {
            "created_at": time.time(), "source_cache": str(source), "fresh_agent_contexts": True,
            "reused_model_checkpoints": 0, "trials": len(protocol["scenes"]) * len(matrix(protocol)),
            "source_protocol_sha256": digest(source / "protocol.json"),
            "source_inputs_sha256": digest(source / "inputs.json"),
            "code_sha256": {str(p.relative_to(REPO)): digest(p) for p in [
                Path(__file__), Path(__file__).with_name("run.py"),
                REPO / "backend/reconstruction_appearance.py", REPO / "backend/world_model.py",
                REPO / "scripts/world_blender.py", REPO / "scripts/review_reconstruction.py"]}})
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.source, args.root))
