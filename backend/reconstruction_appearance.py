"""Explicit appearance objectives shared by builders and independent reviewers."""

RULES = {
    "captured": (
        "本组允许把提供的原图外观映射到物体的真实三维表面，允许纹理包含拍摄时的光照、阴影和高光，"
        "允许appearance=captured。审核不得仅因烘焙光影、缺少重打光能力或移动后保留原阴影要求返修，"
        "也不得强制改成去光照纹理或受光材质。仍须检查实际几何、布局、物体归属、贴图错位和多视角外观；"
        "其他物体的照片残影不属于允许保留的阴影。禁止以朝向相机的整张照片平面代替独立物体。"
    ),
    "intrinsic": (
        "本组禁止整图或局部照片直接投影后充当成品外观，禁止把拍摄光照、投影阴影、高光或邻居物体烘焙进材质。"
        "允许从该物体提取、校正得到的干净固有色/图案纹理，以及程序生成的材质；必须记录纹理来源和处理方法于"
        "该物体evidence.notes与建模源码。各物体独立几何，appearance必须为surface；使用lighting保存可配置灯光，"
        "由Cycles计算照明与随编辑变化的阴影。审核检查纹理归属、光影分离与实际冻结工程，不以自报来源作为通过证据。"
    ),
}


def appearance_instruction(regime):
    return RULES[regime] if regime else "Captured appearance may use only supplied images; no camera-facing photo planes."


def validate_appearance(scene, regime):
    """Check expressible contract fields; texture provenance still needs inspection."""
    if regime == "intrinsic":
        if "lighting" not in scene:
            raise ValueError("intrinsic condition requires explicit scene lighting")
        if any(obj["material"].get("appearance", "surface") != "surface" for obj in scene["objects"]):
            raise ValueError("intrinsic condition requires surface materials")
