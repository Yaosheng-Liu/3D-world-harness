"""RGB-only task packets and camera output alignment for unposed trials."""
from __future__ import annotations

import shutil

import numpy as np

from common import REPO, digest, read, write


CAMERA_FORMAT = """# 输入相机估计的输出格式

相机是本任务的输出，不是已提供的观测。与场景使用相同世界坐标、单位。
在每轮候选旁保存 input_cameras.json：
- camera_convention: OpenGL camera-to-world（相机 -Z 朝前、+Y 朝上）。
- intrinsics: width、height、fx、fy、cx、cy；均为该输入图分辨率下的估计值。
- frames: observations/manifest.json 中全部 54 帧，每项含 index 和 camera_to_world（4×4 刚体矩阵）。

相机与模型一起估计；不要使用数据集发布相机、扫描、外部重建或私有测试信息。
工具组可使用本项目 cameras.json 中工具估计的相机，控制器默认保存该估计。
评测仅用训练帧估计一次全局相似变换，不逐物体配准。退化或缺失相机单列评测失败。
"""


def is_rgb_only(spec):
    return spec.get("input_mode") == "unposed_rgb" and spec["condition"] == "single_basic"


def initialize_run(root, run, spec, runtime, calibrated_initialize):
    """Tools estimate their calibration; the basic arm receives no calibrated stub."""
    if not is_rgb_only(spec):
        project = calibrated_initialize(root, run, spec, runtime)
        note = "\n本实验外部输入只有RGB；本项目相机和观测几何来自DA3工具估计，并非数据集真值。\n"
        with (project / "AGENTS.md").open("a") as stream:
            stream.write(note)
    else:
        project = run / "project"
        project.mkdir()
        shared = root / "shared" / spec["scene"] / "rgb_project"
        shutil.copytree(shared / "observations", project / "observations")
        shutil.copyfile(shared / "training_contact_sheet.jpg", project / "training_contact_sheet.jpg")
        write(project / "experiment.json", spec)
        write(project / "edit_task_targets.json", read(root / "protocol.json")["edit_targets"][spec["scene"]])
        write(project / "tool_runtime.json", {"blender": runtime["blender"],
              "build_timeout_seconds": runtime.get("build_timeout_seconds"),
              "build_glb_limit_bytes": read(root / "protocol.json").get("build_glb_limit_bytes")})
        (project / "AGENTS.md").write_text(
            "根据原始RGB图独立重建并自行修订，物体和可见部件应可独立编辑。无需先交计划或清单。\n"
            "输入是observations/manifest.json与图像、training_contact_sheet.jpg；共同编辑任务见edit_task_targets.json。\n"
            "仅查看本项目和" + str(REPO) + "通用代码，不读其他实验、数据源、扫描或私有评测。\n"
            "允许普通Python、图像读取和Blender；不运行DA3、SAM3、SfM或其他专业空间模型/工具。"
            "不调用额外Agent、外部素材下载、独立审核、叠图或RGB差异材料生成器。\n"
            "保存指定目录的candidate.json、edit_targets.json、建模源码和资产；场景格式见"
            + str(REPO / "backend/world_model.py") + "。没有已初始化场景或参考相机，需自行构建。\n"
            "edit_targets.json将共同目标映射为对象ID；找不到的目标填null。\n"
            "上限256对象、100万视觉三角形。干净物体纹理可用，禁止整张原照片投影或烘入其他物体/光影；"
            "使用lighting字段保存显式灯光，材质appearance=surface；未知面标estimated，物理暂缓。控制器完成最终构建。\n"
            "按需普通预览：python3 " + str(REPO / "experiments/reconstruction/tools.py")
            + " --project " + str(project) + " build --scene <candidate.json>。\n"
            "相机输出约定见CAMERA_OUTPUT.md。\n")
        (project / "TOOLS.md").write_text("只使用普通Blender与代码。不提供专业空间工具。\n")
    (project / "CAMERA_OUTPUT.md").write_text(CAMERA_FORMAT)
    protected = [p for p in (project / "observations").rglob("*") if p.is_file()]
    if (run / "input_hashes.json").exists():
        hashes = read(run / "input_hashes.json")
        hashes.update({name: digest(project / name) for name in ("AGENTS.md", "CAMERA_OUTPUT.md")})
    else:
        names = ("AGENTS.md", "TOOLS.md", "CAMERA_OUTPUT.md", "training_contact_sheet.jpg",
                 "experiment.json", "tool_runtime.json", "edit_task_targets.json")
        protected += [project / name for name in names]
        hashes = {str(p.relative_to(project)): digest(p) for p in protected}
    write(run / "input_hashes.json", hashes)
    return project


def freeze_cameras(project, model_dir, frozen):
    """Keep camera failures visible without discarding a valid modeled scene."""
    path = model_dir / "input_cameras.json"
    spec = read(project / "experiment.json")
    if not path.exists() and not is_rgb_only(spec):
        write(path, read(project / "cameras.json"))
    try:
        data = read(path)
        observations = read(project / "observations/manifest.json")
        expected = {r["index"] for role in ("train", "heldout") for r in observations[role]}
        frames = data["frames"]
        if len(frames) != len(expected) or {r["index"] for r in frames} != expected:
            raise ValueError("Camera output must cover exactly the visible RGB frames")
        matrices = np.asarray([row["camera_to_world"] for row in frames], dtype=float)
        if matrices.shape != (len(expected), 4, 4) or not np.isfinite(matrices).all():
            raise ValueError("Camera estimates must be finite 4x4 matrices")
        rotations = matrices[:, :3, :3]
        if (not np.allclose(matrices[:, 3], [0, 0, 0, 1], atol=1e-5)
                or not np.allclose(rotations @ rotations.transpose(0, 2, 1), np.eye(3), atol=1e-4)
                or not np.allclose(np.linalg.det(rotations), 1, atol=1e-4)):
            raise ValueError("Camera estimates must use proper rigid transforms")
        write(frozen / "input_cameras.json", data)
        status = {"status": "saved", "file": "input_cameras.json", "frames": len(frames)}
    except (OSError, ValueError, KeyError, TypeError) as error:
        if path.exists():
            shutil.copyfile(path, frozen / "input_cameras_unverified.json")
        status = {"status": "invalid", "error": str(error)}
    write(frozen / "camera_output_status.json", status)


def fit_similarity(reference, estimated):
    """Proper Umeyama fit; only camera-center correspondences may be passed."""
    a, b = np.asarray(reference, dtype=float), np.asarray(estimated, dtype=float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 3 or len(a) < 3:
        raise ValueError("At least three corresponding camera centers are required")
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("Camera centers must be finite")
    x, y = a - a.mean(0), b - b.mean(0)
    if np.linalg.matrix_rank(x, tol=1e-7) < 2 or np.linalg.matrix_rank(y, tol=1e-7) < 2:
        raise ValueError("Camera trajectory is degenerate for a global Sim(3)")
    u, values, vt = np.linalg.svd(y.T @ x / len(a))
    signs = np.array([1., 1., np.linalg.det(u @ vt)])
    rotation = (u * signs) @ vt
    scale = float((values * signs).sum() / np.mean(np.sum(x*x, axis=1)))
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("Alignment must have positive scale")
    translation = b.mean(0) - scale * rotation @ a.mean(0)
    residual = np.linalg.norm(scale * (a @ rotation.T) + translation - b, axis=1)
    return {"scale": scale, "rotation": rotation.tolist(), "translation": translation.tolist(),
            "camera_center_rmse_in_estimated_units": float(np.sqrt(np.mean(residual**2)))}


def transform_cameras(cameras, alignment):
    result = np.asarray(cameras, dtype=float).copy()
    rotation = np.asarray(alignment["rotation"])
    result[:, :3, :3] = rotation @ result[:, :3, :3]
    result[:, :3, 3] = alignment["scale"] * (result[:, :3, 3] @ rotation.T) + alignment["translation"]
    return result
