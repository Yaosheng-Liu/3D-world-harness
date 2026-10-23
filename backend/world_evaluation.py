"""Full-frame image measurements for fixed, source-bound reconstruction views.

These appearance errors are not geometry accuracy or physical calibration.
Evaluation is read-only; contact sheets are an explicit, separate operation.
"""

import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from world_model import WorldError, resolve_source_path, scene_hash


def _image(path, expected_hash, *, reference=False):
    data = path.read_bytes()
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        raise WorldError(f"Image changed or was not bound to the build: {path.name}")
    with Image.open(io.BytesIO(data)) as source:
        if source.mode not in ("RGB", "RGBA"):
            raise WorldError(f"Evaluation requires 8-bit RGB or RGBA images: {path}")
        image = source.copy()
    coverage = None
    if image.mode == "RGBA":
        alpha = image.getchannel("A")
        histogram = alpha.histogram()
        if reference and histogram[255] != image.width * image.height:
            raise WorldError("Reference images must be opaque")
        coverage = sum(histogram[128:]) / (image.width * image.height)
        image = Image.composite(image.convert("RGB"), Image.new("RGB", image.size, "white"), alpha)
    return image, coverage, actual_hash


def _metrics(absolute_error):
    histogram = absolute_error.histogram()
    count = absolute_error.width * absolute_error.height * 3
    mae = sum((index % 256) * frequency for index, frequency in enumerate(histogram)) / (count * 255)
    mse = sum((index % 256) ** 2 * frequency for index, frequency in enumerate(histogram)) / (count * 255 ** 2)
    return {"mae": mae, "mse": mse, "psnr_db": _psnr(mse), "rgb_component_count": count}


def _psnr(mse):
    return None if mse == 0 else -10 * math.log10(mse)


def _evaluate(project_dir, scene, build):
    views = scene["views"]
    if not any(view.get("reference_image") for view in views):
        return {"status": "not_measured", "reason": "No reference images are assigned to views."}, []
    paths = build.get("render_paths", [])
    if not paths:
        return {"status": "not_measured", "reason": "This build has no rendered views."}, []
    try:
        digest = scene_hash(scene)
    except (TypeError, ValueError) as exc:
        raise WorldError("Evaluation scene must contain only finite JSON values") from exc
    if build["scene_hash"] != digest:
        raise WorldError("Evaluation cameras and scene differ from the built revision")
    if len(paths) != len(views):
        raise WorldError("Rendered view count differs from the scene")
    project = Path(project_dir).resolve()
    build_dir = Path(build["build_dir"]).resolve()
    if not build_dir.is_relative_to(project):
        raise WorldError("Evaluation build directory escapes the project")
    records, panels = [], []
    for view, render_name in zip(views, paths):
        if not view.get("reference_image"):
            continue
        reference_path = resolve_source_path(view["reference_image"], project)
        render_path = Path(render_name).resolve()
        if not render_path.is_relative_to(build_dir) or render_path.stem != view["id"]:
            raise WorldError(f"Rendered image does not match view: {view['id']}")
        reference, _, reference_hash = _image(
            reference_path, build.get("asset_hashes", {}).get(view["reference_image"]), reference=True)
        rendered, coverage, render_hash = _image(
            render_path, build.get("files", {}).get(str(render_path.relative_to(build_dir))))
        intrinsics = view["intrinsics"]
        if reference.size != rendered.size or reference.size != (intrinsics["width"], intrinsics["height"]):
            raise WorldError(f"Reference/render sizes must equal camera intrinsics: {view['id']}")
        error = ImageChops.difference(reference, rendered)
        records.append({"view_id": view["id"], "reference_image": view["reference_image"],
                        "reference_sha256": reference_hash, "render_sha256": render_hash,
                        "width": reference.width, "height": reference.height,
                        "alpha_coverage": coverage, **_metrics(error)})
        panels.append((view["id"], reference, rendered, error))
    count = sum(record["rgb_component_count"] for record in records)
    mae = sum(record["mae"] * record["rgb_component_count"] for record in records) / count
    mse = sum(record["mse"] * record["rgb_component_count"] for record in records) / count
    report = {"status": "measured", "scene_hash": digest, "revision": scene["revision"],
              "view_count": len(records), "views": records,
              "aggregate": {"mae": mae, "mse": mse, "psnr_db": _psnr(mse), "rgb_component_count": count},
              "method": {"domain": "8-bit encoded RGB divided by 255; no exposure or color fitting",
                         "denominator": "All RGB components in all pixels; transparent regions are not masked",
                         "background_rgb": [1, 1, 1],
                         "alpha": "RGBA renders are composited onto white; coverage is alpha > 127/255, not geometry coverage",
                         "perfect_psnr": "psnr_db is null when MSE is zero (identical images)",
                         "scope": "Image appearance only; not geometry accuracy, metric scale, or a quality pass"}}
    json.dumps(report, allow_nan=False)
    return report, panels


def evaluate_views(project_dir, scene, build):
    """Measure available references without writing or modifying any artifact."""
    return _evaluate(project_dir, scene, build)[0]


def _inspection_path(directory, filename):
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory):
        raise WorldError("Evaluation output file escapes its directory")
    return path


def write_view_inspection(project_dir, scene, build):
    """Explicitly write source/render/overlay/error sheets and their measurements.

    Inspect held-out images only after freezing this revision. Do not feed them
    back into optimization and continue describing that revision as held-out.
    """
    report, panels = _evaluate(project_dir, scene, build)
    if report["status"] != "measured":
        return report
    output = (Path(build["build_dir"]).resolve() / "evaluation").resolve()
    if not output.is_relative_to(Path(build["build_dir"]).resolve()):
        raise WorldError("Evaluation output escapes the build directory")
    output.mkdir(exist_ok=True)
    sheets = []
    for view_id, reference, rendered, error in panels:
        width, height = reference.size
        sheet = Image.new("RGB", (width * 4, height + 24), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (label, panel) in enumerate((("Reference", reference), ("Render", rendered),
                                               ("50% overlay", Image.blend(reference, rendered, 0.5)),
                                               ("Absolute RGB error (no amplification)", error))):
            sheet.paste(panel, (index * width, 24))
            draw.text((index * width + 4, 4), label, fill="black")
        path = _inspection_path(output, f"{view_id}.png")
        sheet.save(path)
        sheets.append(str(path))
    report["inspection_paths"] = sheets
    _inspection_path(output, "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return report
