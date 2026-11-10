#!/usr/bin/env python3
"""Evaluate a world-coordinate mesh against independent sparse multiview observations.

Reference NPZ arrays: points/origins/directions (N, 3), depths/frame_indices/
reprojection_errors (N,). Directions are unit world rays, depths are ray lengths,
and points must agree with origins + directions * depths. No alignment is fitted.
Open3D is required only for mesh evaluation; statistics use NumPy alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


THRESHOLDS = (0.01, 0.02, 0.05, 0.10)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_reference(path):
    names = ("points", "origins", "directions", "depths", "frame_indices",
             "reprojection_errors")
    with np.load(path, allow_pickle=False) as archive:
        missing = set(names) - set(archive.files)
        if missing:
            raise ValueError(f"Missing reference arrays: {sorted(missing)}")
        data = {name: np.asarray(archive[name]) for name in names}
    n = len(data["depths"]) if data["depths"].ndim else 0
    if not n:
        raise ValueError("Reference must contain at least one observation")
    for name, values in data.items():
        shape = (n, 3) if name in ("points", "origins", "directions") else (n,)
        if values.shape != shape or values.dtype.kind not in "fiu":
            raise ValueError(f"{name} must be a numeric array of shape {shape}")
        if not np.isfinite(values).all():
            raise ValueError(f"{name} contains nonfinite values")
        data[name] = values.astype(np.float64)
    if np.any(data["depths"] <= 0) or np.any(data["reprojection_errors"] < 0):
        raise ValueError("Depths must be positive and reprojection errors nonnegative")
    frames = data["frame_indices"]
    if np.any(frames < 0) or np.any(frames != np.floor(frames)) or np.any(frames >= 2**53):
        raise ValueError("frame_indices must contain exactly representable nonnegative integers")
    data["frame_indices"] = frames.astype(np.int64)
    norms = np.linalg.norm(data["directions"], axis=1)
    if np.max(np.abs(norms - 1)) > 1e-4:
        raise ValueError("Reference directions must be unit world rays (tolerance 1e-4)")
    data["directions"] /= norms[:, None]
    expected = data["origins"] + data["directions"] * data["depths"][:, None]
    residual = np.linalg.norm(data["points"] - expected, axis=1) / data["depths"]
    if np.max(residual) > 2e-4:
        raise ValueError("Reference points disagree with origins + directions * depths")
    data["point_ray_relative_residual_max"] = float(residual.max())
    return data


def distribution(values):
    """Return explicit null statistics for an empty conditional population."""
    values = np.asarray(values, dtype=float)
    if not len(values):
        return {"count": 0, "mean": None, "median": None, "p90": None,
                "p95": None, "maximum": None}
    return {"count": int(len(values)), "mean": float(values.mean()),
            "median": float(np.median(values)), "p90": float(np.quantile(values, .90)),
            "p95": float(np.quantile(values, .95)), "maximum": float(values.max())}


def summarize(depths, hit_lengths, surface_distances):
    """All accuracy rates and capped errors keep missing observations in denominator."""
    depths = np.asarray(depths, dtype=float)
    hit_lengths = np.asarray(hit_lengths, dtype=float)
    surface_distances = np.asarray(surface_distances, dtype=float)
    if not len(depths) or hit_lengths.shape != depths.shape or surface_distances.shape != depths.shape:
        raise ValueError("Metric arrays must have identical, nonempty one-dimensional shapes")
    if depths.ndim != 1 or not np.isfinite(depths).all() or np.any(depths <= 0):
        raise ValueError("Metric reference depths must be finite and positive")
    hit = np.isfinite(hit_lengths) & (hit_lengths >= 0)
    nearest = np.isfinite(surface_distances) & (surface_distances >= 0)
    signed = np.full(len(depths), np.nan)
    signed[hit] = (hit_lengths[hit] - depths[hit]) / depths[hit]
    absolute = np.abs(signed)
    relative_distance = np.full(len(depths), np.nan)
    relative_distance[nearest] = surface_distances[nearest] / depths[nearest]
    ray_penalty = np.ones(len(depths))
    ray_penalty[hit] = np.minimum(absolute[hit], 1.)
    surface_penalty = np.ones(len(depths))
    surface_penalty[nearest] = np.minimum(relative_distance[nearest], 1.)
    accuracy = {f"within_{int(t * 100)}pct": float(np.mean(hit & (absolute <= t)))
                for t in THRESHOLDS}
    proximity = {f"within_{int(t * 100)}pct": float(np.mean(nearest & (relative_distance <= t)))
                 for t in THRESHOLDS}
    return {
        "reference_observations": int(len(depths)),
        "first_hit": {
            "hit_count": int(hit.sum()), "missing_count": int((~hit).sum()),
            "coverage": float(hit.mean()),
            "conditional_on_hit_relative_absolute_error": distribution(absolute[hit]),
            "conditional_on_hit_relative_signed_error": distribution(signed[hit]),
            "capped_relative_error_including_missing": float(ray_penalty.mean()),
            "hit_and_correct_fraction_of_all_reference_rays": accuracy,
        },
        "point_to_surface": {
            "valid_count": int(nearest.sum()), "missing_count": int((~nearest).sum()),
            "conditional_on_valid_distance_dataset_units": distribution(surface_distances[nearest]),
            "conditional_on_valid_relative_distance": distribution(relative_distance[nearest]),
            "capped_relative_distance_including_missing": float(surface_penalty.mean()),
            "within_threshold_fraction_of_all_reference_points": proximity,
        },
    }


def frame_metric_vector(summary):
    """Macro/bootstrap metrics never average away frames with missing geometry."""
    ray, surface = summary["first_hit"], summary["point_to_surface"]
    metrics = {
        "ray_coverage": ray["coverage"],
        "first_hit_capped_relative_error_including_missing": ray["capped_relative_error_including_missing"],
        "point_to_surface_capped_relative_distance_including_missing": surface["capped_relative_distance_including_missing"],
    }
    for name, value in ray["hit_and_correct_fraction_of_all_reference_rays"].items():
        metrics[f"first_hit_accuracy_{name}"] = value
    for name, value in surface["within_threshold_fraction_of_all_reference_points"].items():
        metrics[f"point_to_surface_accuracy_{name}"] = value
    # Uncapped nearest-surface distance is defined only if all references have a distance.
    metrics["point_to_surface_mean_relative_distance"] = (
        surface["conditional_on_valid_relative_distance"]["mean"]
        if surface["missing_count"] == 0 else None)
    return metrics


def macro_and_bootstrap(per_frame, samples, seed):
    vectors = [frame_metric_vector(row["metrics"]) for row in per_frame]
    names = list(vectors[0])
    values = np.array([[np.nan if row[name] is None else row[name] for name in names]
                       for row in vectors], dtype=float)
    complete = np.isfinite(values).all(axis=0)
    macro = {name: float(values[:, i].mean()) if complete[i] else None
             for i, name in enumerate(names)}
    bootstrap = {"unit": "source frame, equal weight per frame", "samples": int(samples),
                 "seed": int(seed), "frame_count": len(per_frame),
                 "interpretation": "Descriptive frame-resampling percentile intervals only. Adjacent frames and repeated tracks are correlated; these are not significance tests or population confidence claims.",
                 "percentile_intervals_95": {}}
    if samples:
        rng = np.random.default_rng(seed)
        selected = values[:, complete]
        replicas = np.empty((samples, selected.shape[1]))
        for first in range(0, samples, 128):
            stop = min(first + 128, samples)
            indices = rng.integers(0, len(per_frame), size=(stop - first, len(per_frame)))
            replicas[first:stop] = selected[indices].mean(axis=1)
        intervals = np.quantile(replicas, [.025, .975], axis=0)
        j = 0
        for i, name in enumerate(names):
            if complete[i]:
                bootstrap["percentile_intervals_95"][name] = {
                    "lower": float(intervals[0, j]), "upper": float(intervals[1, j])}
                j += 1
            else:
                bootstrap["percentile_intervals_95"][name] = None
    return macro, bootstrap


def evaluate_mesh(mesh_path, reference, chunk_size, threads):
    import open3d as o3d

    mesh = o3d.io.read_triangle_mesh(str(mesh_path))
    vertices = np.asarray(mesh.vertices)
    triangles = np.asarray(mesh.triangles)
    if not np.isfinite(vertices).all():
        raise ValueError("Mesh contains nonfinite vertices")
    if triangles.size and (triangles.min() < 0 or triangles.max() >= len(vertices)):
        raise ValueError("Mesh contains invalid vertex indices")
    n = len(reference["depths"])
    hits, distances = np.full(n, np.inf), np.full(n, np.inf)
    info = {"open3d_version": o3d.__version__, "vertices": len(vertices),
            "input_triangles": len(triangles), "degenerate_triangles_removed": 0,
            "empty_geometry": not (len(vertices) and len(triangles))}
    if len(triangles):
        corners = vertices[triangles]
        area_vectors = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        valid = np.any(area_vectors != 0, axis=1)
        info["degenerate_triangles_removed"] = int((~valid).sum())
        triangles = triangles[valid]
    info["evaluated_triangles"] = len(triangles)
    if not len(triangles):
        info["empty_geometry"] = True
        info["warning"] = "No usable triangles were loaded; all observations receive the missing penalty of 1."
        return hits, distances, info
    # RaycastingScene uses float32. A common translation reduces large-origin rounding
    # without fitting any scale, rotation, or registration to the reference points.
    origin_shift = np.median(reference["origins"], axis=0)
    shifted_vertices = np.asarray(vertices - origin_shift, dtype=np.float32)
    if not np.isfinite(shifted_vertices).all():
        raise ValueError("Mesh coordinates exceed float32 evaluation range")
    scene = o3d.t.geometry.RaycastingScene(nthreads=threads)
    scene.add_triangles(o3d.core.Tensor(shifted_vertices, dtype=o3d.core.Dtype.Float32),
                        o3d.core.Tensor(triangles.astype(np.uint32), dtype=o3d.core.Dtype.UInt32))
    for start in range(0, n, chunk_size):
        stop = min(start + chunk_size, n)
        rays = np.concatenate([reference["origins"][start:stop] - origin_shift,
                               reference["directions"][start:stop]], axis=1).astype(np.float32)
        points = (reference["points"][start:stop] - origin_shift).astype(np.float32)
        if not np.isfinite(rays).all() or not np.isfinite(points).all():
            raise ValueError("Reference coordinates exceed float32 evaluation range")
        ray_tensor = o3d.core.Tensor(rays, dtype=o3d.core.Dtype.Float32)
        point_tensor = o3d.core.Tensor(points, dtype=o3d.core.Dtype.Float32)
        # t_hit scales by the supplied direction length, so convert it to ray length.
        hits[start:stop] = (scene.cast_rays(ray_tensor, nthreads=threads)["t_hit"].numpy()
                            * np.linalg.norm(rays[:, 3:].astype(np.float64), axis=1))
        distances[start:stop] = scene.compute_distance(point_tensor, nthreads=threads).numpy()
    info["common_numerical_origin_shift"] = origin_shift.tolist()
    info["registration"] = "None; identical translation applied to mesh and rays for float32 precision only"
    return hits, distances, info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--mesh", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-size", type=int, default=262144)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument("--threads", type=int, default=0)
    args = parser.parse_args()
    if args.chunk_size < 1 or args.bootstrap_samples < 0 or args.threads < 0 or args.seed < 0:
        parser.error("chunk-size must be positive; bootstrap-samples, threads, and seed must be nonnegative")
    for path in (args.reference, args.mesh):
        if not path.is_file():
            parser.error(f"Input file does not exist: {path}")
    if args.output.resolve() in {args.reference.resolve(), args.mesh.resolve()}:
        parser.error("Output must not overwrite either input")
    reference = load_reference(args.reference)
    hits, distances, mesh_info = evaluate_mesh(args.mesh, reference, args.chunk_size, args.threads)
    per_frame = []
    for frame in np.unique(reference["frame_indices"]):
        selected = reference["frame_indices"] == frame
        per_frame.append({"frame_index": int(frame),
                          "reference_reprojection_error": distribution(reference["reprojection_errors"][selected]),
                          "metrics": summarize(reference["depths"][selected], hits[selected], distances[selected])})
    macro, bootstrap = macro_and_bootstrap(per_frame, args.bootstrap_samples, args.seed)
    report = {
        "schema_version": 1,
        "reference_kind": "Sparse multiview triangulation pseudo-reference; not ground truth and not a full-surface assessment",
        "limitations": [
            "Reference selection and filtering must be independent of both candidate meshes. This script cannot verify that provenance.",
            "Only supplied reference rays/points are evaluated; textureless, occluded, and unobserved surfaces may be absent. Missing source frames are not evaluated.",
            "First-hit coverage measures the fraction of sparse reference rays hitting geometry, not full-image completeness. It must be read with depth accuracy.",
            "Point-to-surface distance is one-sided and cannot penalize every extra surface; first-hit ray errors expose some foreground occlusions.",
            "Hit-only errors are conditional diagnostics and must never rank models without coverage and the missing-inclusive scores.",
            "Scale remains dataset camera-translation units. Normalization uses each reference ray length; no fitted alignment or scale is applied.",
            "Frames and observations of the same tracks can be correlated; bootstrap intervals are descriptive only.",
        ],
        "definitions": {
            "relative_point_to_surface_distance": "unsigned Euclidean closest-surface distance / reference ray length",
            "relative_first_hit_error": "abs(first intersection ray length - reference ray length) / reference ray length",
            "missing_inclusive_capped_error": "mean(min(relative error, 1)); missing or invalid intersection/distance contributes exactly 1",
            "accuracy_thresholds": "Fraction of ALL supplied observations with a valid hit/distance and relative error <= threshold",
            "macro_average": "Arithmetic mean of per-frame metric values, equal weight per observed source frame; no missing-hit frames dropped",
            "pooled": "All supplied observations pooled; a track observed in several views contributes several observations",
            "reprojection_error_units": "Preserved from reference producer; no unit conversion or filtering is performed",
        },
        "inputs": {"reference": str(args.reference.resolve()), "reference_sha256": sha256(args.reference),
                   "mesh": str(args.mesh.resolve()), "mesh_sha256": sha256(args.mesh),
                   "script_sha256": sha256(__file__)},
        "reference_validation": {"observations": len(reference["depths"]), "frames": len(per_frame),
                                 "point_ray_relative_residual_max": reference["point_ray_relative_residual_max"],
                                 "reprojection_error": distribution(reference["reprojection_errors"])},
        "mesh": mesh_info,
        "pooled_metrics": summarize(reference["depths"], hits, distances),
        "per_frame": per_frame,
        "macro_average_metrics": macro,
        "descriptive_bootstrap_across_frames": bootstrap,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.output.resolve()), "observations": len(reference["depths"]),
                      "frames": len(per_frame), "macro_average_metrics": macro}, allow_nan=False))


if __name__ == "__main__":
    main()
