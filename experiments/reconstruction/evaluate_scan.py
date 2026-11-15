"""Observed-surface scan metrics after a single camera-only alignment."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from common import digest, write
from evaluate_sparse import evaluate_mesh, load_reference, macro_and_bootstrap, summarize


def evaluate(reference_path, mesh_path, scale):
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("Metric conversion requires positive finite alignment scale")
    reference = load_reference(reference_path)
    hits, distances, mesh_info = evaluate_mesh(mesh_path, reference, 262144, 4)
    rows = []
    for frame in np.unique(reference["frame_indices"]):
        selected = reference["frame_indices"] == frame
        rows.append({"frame_index": int(frame), "metrics": summarize(reference["depths"][selected], hits[selected], distances[selected])})
    macro, bootstrap = macro_and_bootstrap(rows, 2000, 20261115)
    # Absolute errors are returned to released-scan meters. Missing hits stay in threshold denominators.
    for cm in (5, 10):
        threshold = cm/100
        ray_values, surface_values = [], []
        for frame in np.unique(reference["frame_indices"]):
            selected = reference["frame_indices"] == frame
            ray_error = np.abs(hits[selected]-reference["depths"][selected])/scale
            surface_error = distances[selected]/scale
            ray_values.append(float(np.mean(np.isfinite(ray_error) & (ray_error <= threshold))))
            surface_values.append(float(np.mean(np.isfinite(surface_error) & (surface_error <= threshold))))
        macro[f"first_hit_accuracy_{cm}cm"] = float(np.mean(ray_values))
        macro[f"point_to_surface_accuracy_{cm}cm"] = float(np.mean(surface_values))
    return {"schema_version": 1, "reference_kind": "ScanNet++ released aligned laser mesh, visible first-hit samples",
            "alignment_scale": scale, "units": "Relative errors are scale invariant; cm thresholds use released scan meters",
            "limitations": ["Visible sampled scan surfaces only; no full-scene symmetric Chamfer or instance ground truth.",
                            "Missing intersections count as errors; conditional diagnostics do not replace coverage.",
                            "Scan mirror lacks anonymization mask and official release hash cross-check."],
            "inputs": {"reference_sha256": digest(reference_path), "mesh_sha256": digest(mesh_path)},
            "mesh": mesh_info, "per_frame": rows, "macro_average_metrics": macro,
            "pooled_metrics": summarize(reference["depths"], hits, distances),
            "descriptive_bootstrap_across_frames": bootstrap}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--mesh", type=Path, required=True)
    parser.add_argument("--scale", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write(args.output, evaluate(args.reference, args.mesh, args.scale))
