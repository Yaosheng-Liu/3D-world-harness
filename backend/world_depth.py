"""Compare a built candidate with fixed DA3 camera-Z estimates, without fitting."""
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw

from reconstruction_review import project_file, read_json, verify_files, write_json
from world_build import file_hash
from world_model import scene_hash


CONVENTION = "OpenCV camera Z, not Euclidean ray distance"
WORKER = Path(__file__).resolve().parents[1] / "scripts/depth_inspection_blender.py"


def depth_panels(reference, model, confidence, *, depth_max, error_limit):
    """Shared color scales; unknown references and missing model surfaces stay visible."""
    import numpy as np
    valid = np.isfinite(reference) & (reference > 0) & np.isfinite(confidence) & (confidence > 0)
    threshold = float(np.percentile(confidence[valid], 20)) if valid.any() else None
    trusted = valid & (confidence >= threshold) if threshold is not None else valid
    hit = np.isfinite(model) & (model > 0)
    compared = trusted & hit
    panels = []
    for depth, mask in ((reference, trusted), (model, hit)):
        gray = np.clip(np.nan_to_num(depth) / depth_max, 0, 1)
        rgb = np.repeat((gray[..., None] * 255).astype(np.uint8), 3, axis=2)
        rgb[~mask] = (90, 90, 90)
        panels.append(Image.fromarray(rgb))
    # Red: candidate farther from this camera. Blue: candidate nearer.
    difference = model - reference
    amount = np.clip(np.nan_to_num(abs(difference)) / error_limit, 0, 1)
    colors = np.where((difference >= 0)[..., None], [220, 45, 35], [35, 95, 220])
    rgb = (255 * (1 - amount[..., None]) + colors * amount[..., None]).astype(np.uint8)
    rgb[~trusted] = (90, 90, 90)
    rgb[trusted & ~hit] = (200, 35, 200)
    panels.append(Image.fromarray(rgb))
    return panels, {"confidence_p20": threshold, "reference_pixels": int(trusted.sum()),
                    "compared_pixels": int(compared.sum()), "missing_model_pixels": int((trusted & ~hit).sum())}


def write_depth_inspection(project, scene, build, output, *, blender="blender", frames=None, timeout=1800):
    """Generate fresh evidence; never overwrite a previous review or invoke DA3 again."""
    project, output = Path(project).resolve(), Path(output).resolve()
    if not output.is_relative_to(project):
        raise ValueError("depth inspection output must be inside the project")
    inference_path = project / "dense/inference.json"
    if not inference_path.exists():
        return {"status": "unavailable", "reason": "No cached DA3 reference depth in this project."}
    import numpy as np
    digest = scene_hash(scene)
    if build["scene_hash"] != digest:
        raise ValueError("depth inspection build differs from candidate")
    inference = read_json(project_file(project, "dense/inference.json"))
    if inference["depth_convention"] != CONVENTION:
        raise ValueError("unsupported reference depth convention")
    # Bind input bytes before loading. No alignment is estimated from the candidate.
    inputs = {"dense/inference.json": file_hash(inference_path),
              "dense/depths.npz": inference["depths_sha256"],
              "observations/manifest.json": inference["prepared_manifest_sha256"]}
    for name in ("dense/inference.json", "observations/manifest.json", "reconstruction_report.json"):
        if build["asset_hashes"].get(name) != file_hash(project_file(project, name)):
            raise ValueError(f"candidate does not bind the reference coordinate evidence: {name}")
        inputs[name] = build["asset_hashes"][name]
    coordinates = read_json(project_file(project, "reconstruction_report.json"))
    if not np.allclose(coordinates["source_to_world"], np.eye(4), atol=1e-8):
        raise ValueError("depth inspection currently requires the original DA3 world coordinates")
    if coordinates["units"] != scene["units"]:
        raise ValueError("candidate and reference depth units differ")
    blend = project_file(project, build["blend_path"])
    blend_key = str(blend.relative_to(project_file(project, Path(build["build_dir"]) / "job.json").parent))
    inputs[str(blend.relative_to(project))] = build["files"][blend_key]
    verify_files(project, inputs)
    with np.load(project_file(project, "dense/depths.npz"), allow_pickle=False) as archive:
        indices = archive["frame_indices"].tolist()
        if indices != inference["training_indices"] or len(set(indices)) != len(indices) or not indices:
            raise ValueError("DA3 frames differ from its frozen training set")
        selected = frames if frames is not None else [indices[i] for i in np.linspace(0, len(indices)-1, min(6, len(indices)), dtype=int)]
        if not selected or len(set(selected)) != len(selected) or any(i not in indices for i in selected):
            raise ValueError("depth frames must be unique cached DA3 training frames")
        offsets = [indices.index(i) for i in selected]
        depth, confidence = archive["depth"][offsets], archive["confidence"][offsets]
        intrinsics, extrinsics = archive["intrinsics"][offsets], archive["extrinsics"][offsets]
        if depth.ndim != 3 or confidence.shape != depth.shape or not np.isfinite(depth).all() or not (depth > 0).all():
            raise ValueError("invalid cached reference depth or confidence shape")
        if (intrinsics.shape != (len(selected), 3, 3) or extrinsics.shape != (len(selected), 4, 4)
                or not np.isfinite(intrinsics).all() or not np.isfinite(extrinsics).all()):
            raise ValueError("invalid cached DA3 cameras")
        # Fixed visualization scales depend on the full reference cache, never the candidate.
        depth_max = float(np.percentile(archive["depth"], 98))
        error_limit = float(np.median(archive["depth"])) * .1
    observations = {row["index"]: row for row in read_json(project / "observations/manifest.json")["train"]}
    for i in selected:
        row = observations[i]
        inputs[row["path"]] = row["sha256"]
    verify_files(project, inputs)
    output.mkdir(parents=True, exist_ok=False)
    job = {"blend_path": str(blend), "scene_hash": digest, "output_dir": str(output),
           "width": int(depth.shape[2]), "height": int(depth.shape[1]), "frame_indices": selected,
           "intrinsics": intrinsics.tolist(), "extrinsics": extrinsics.tolist()}
    write_json(output / "job.json", job)
    with (output / "blender.log").open("w") as log:
        subprocess.run([blender, "--background", "--factory-startup", "--python-exit-code", "1",
                        "--python", str(WORKER), "--", str(output / "job.json")],
                       check=True, timeout=timeout, stdout=log, stderr=subprocess.STDOUT)
    with np.load(output / "model_depth.npz", allow_pickle=False) as archive:
        model = archive["depth"]
        if model.shape != depth.shape or archive["frame_indices"].tolist() != selected:
            raise ValueError("Blender depth does not match the requested cameras")
        if not np.isfinite(model).all() or (model < 0).any():
            raise ValueError("invalid Blender camera-Z depth")
    rows = []
    for index, frame in enumerate(selected):
        panels, counts = depth_panels(depth[index], model[index], confidence[index],
                                      depth_max=depth_max, error_limit=error_limit)
        width, height = panels[0].size
        sheet = Image.new("RGB", (width * 3, height + 64), "white")
        draw = ImageDraw.Draw(sheet)
        for j, (label, panel) in enumerate(zip(("DA3 reference estimate", "Blender first surface", "Model Z - reference Z"), panels)):
            sheet.paste(panel, (j * width, 24))
            draw.text((j * width + 4, 4), label, fill="black")
        draw.text((4, height + 28), f"source_{frame} | depth: black=0, white={depth_max:.4g} {scene['units']} | delta saturation: +/-{error_limit:.4g}", fill="black")
        draw.text((4, height + 46), "Blue=nearer  Red=farther  White=equal  Gray=unknown/low confidence  Magenta=missing model", fill="black")
        path = output / f"source_{frame}.png"
        sheet.save(path)
        rows.append({"view_id": f"source_{frame}", "frame_index": frame, "reference_image": observations[frame]["path"],
                     "inspection_path": str(path.relative_to(project)), **counts})
    verify_files(project, inputs)
    report = {"status": "measured", "scene_hash": digest, "units": scene["units"], "depth_convention": CONVENTION,
              "method": "First intersection of evaluated visual meshes, at DA3 pixel coordinates; no scale/pose fitting.",
              "limitations": "DA3 is estimated evidence, also used for initialization. Confidence filtering is relative, not calibrated reliability. Mirrors, transparency, shader displacement and unseen surfaces require original multi-view evidence. Positive/negative differences indicate camera-relative distance, not proof of concavity or convexity.",
              "depth_max": depth_max, "error_limit": error_limit, "confidence_percentile": 20,
              "views": rows, "inspection_paths": [row["inspection_path"] for row in rows],
              "input_files": inputs, "files": {str(f.relative_to(project)): file_hash(f)
                  for f in sorted(output.iterdir()) if f.suffix in {".png", ".npz", ".json"}}}
    write_json(output / "report.json", report)
    return report
