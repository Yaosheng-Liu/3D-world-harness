# 外观规则和正式渲染

新实验从原始输入重新建模，区分两种独立协议。`captured`允许把训练图外观贴到物体表面，包含已有光影；审核不要求重打光。`intrinsic`禁止直接照片投影，允许物体自身提取并校正的干净纹理或程序纹理，几何、纹理归属、受光材质与灯光需要配合。干净纹理的自报来源不能代替视觉核验。

两组所有条件使用同一简短任务。单Agent无独立计划步骤、物体清单或外部审核反馈；按需读取工具和格式说明。模型、计划与审核统一ultra，仍是M0→R1→M1→R2→M2；旧候选和旧会话不复用。可复用同一份训练图/相机及DA3/TSDF观测缓存，其来源和原计算成本单列。

## 灯光进入交付工程

候选可增加顶层`lighting`。存在该字段时使用所列灯光，不再额外放置通用Key/Fill灯；没有该字段的旧场景保留原行为。灯光保存到正式`scene.blend`，供构建预览、冻结审核和私有Cycles评测共同使用。GLB继续交付几何和材质，未导出灯光。

```json
{
  "lighting": {
    "environment": {"color": [0.8, 0.85, 1.0], "strength": 0.1},
    "lights": [
      {"id": "ceiling_key", "type": "area", "position": [0, 0, 3],
       "rotation": [0, 0, 0], "color": [1, 0.95, 0.9], "energy": 200, "size": 2},
      {"id": "lamp_emitter", "type": "spot", "parent_id": "table_lamp",
       "position": [0, 0, 0.2], "rotation": [0, 0, 0],
       "color": [1, 0.9, 0.75], "energy": 30, "size": 0.05,
       "spot_size": 0.8, "spot_blend": 0.5}
    ]
  },
  "color_management": {"view_transform": "Standard", "exposure": 0}
}
```

灯光类型为`area`、`point`、`spot`。旋转是XYZ欧拉弧度，面光和聚光朝局部-Z；位置/尺寸沿用场景单位，能量使用Blender灯光参数。`size`表示面光直径或点/聚光软阴影半径。指定`parent_id`时变换相对物体，灯光随父物体移动；不指定时为世界坐标。例子中的位置与能量仅说明字段，必须根据实际房间坐标、尺度和观察拟合。

材质继续用`base_color`、`roughness`、`metallic`和可选`base_color_texture`。`surface`另支持`emission_color`（RGB）、`emission_strength`、`transmission`（0～1）与`ior`。发光用于真实灯具；不应用发光材质模拟普通表面的照片外观。`intrinsic`条件须显式提供lighting并使用surface材质；程序仅检查这些可表达的条件，是否夹带原图光影仍需要检查源码和纹理。

两个新实验都固定Standard、曝光0、gamma1，避免材质切换自动改变色彩映射；灯光强度与材质参数仍可拟合。所有原始数据只读，私有测试不用于调参。

## 启动新批次

```bash
python3 experiments/reconstruction/prepare_appearance.py --source OLD_BATCH --root NEW_PARENT
```

生成`NEW_PARENT/captured`与`NEW_PARENT/intrinsic`两个独立实验根目录，每个为三个场景×两个模型×三种流程，共18次。工具观测缓存共享，项目和Agent会话全部新建，初始任务相同；只有明确登记的外观规则不同。每个根目录复用run、monitor、evaluate、report、sync_records入口；两边各设置每模型2个槽位，合计最多8个试验并行。计划、分割与几何初始化按新会话需要重新执行，私有评测沿用相同数据划分。
