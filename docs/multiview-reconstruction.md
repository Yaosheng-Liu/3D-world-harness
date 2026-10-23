# 真实多视角重建

本轮提供一条**已知相机参数**的重建流程：真实训练图像 → 相机条件深度 → 多视角一致性过滤 → TSDF 网格 → 训练图像纹理 → Blender/GLB → 留出视角图像评价。输入相机来自已有 SfM；本脚本尚不为无相机的照片集合求解位姿。

## 输入与尺度

输入目录包含 Nerfstudio 格式 `transforms.json` 和其中引用的图像。当前支持全序列共享内参的 PINHOLE/OPENCV 相机；逐帧内参、鱼眼和高阶畸变会明确拒绝。相机矩阵采用 OpenGL camera-to-world；焦距、主点和图像宽高必须对应实际图像，不重复应用已有坐标变换或去畸变。脚本用训练图像的特征匹配比较保留原图与应用所记录畸变两种投影，记录选择及残差。

默认先固定48个训练视角、6个留出视角。留出RGB不进入深度推理、融合或纹理；相机可能由包含所有图像的SfM得到，因此这不是独立相机真值验证。实际观察过的候选图像另记来源。

未标定尺度保存为 `units: arbitrary`。网格、相机和观测使用相同坐标，并记录 `source_to_world`；MuJoCo拒绝此类世界，不能把一个数据单位默认为一米。物品分割、未观测背面补全和质量/摩擦标定是后续步骤。

## 运行

重建环境需要 Depth Anything 3 的代码与本地权重、PyTorch CUDA、OpenCV、Open3D、NumPy。普通工作台和世界编辑不需要加载这些依赖。此项目已在spark的 `mv-sam3d:gb10-da3` 环境验证；模型与原图保持在计算端，不打入插件包。

```bash
python scripts/reconstruct_multiview.py --scene /path/to/source --output /path/to/project --stage prepare
python scripts/reconstruct_multiview.py --scene /path/to/source --output /path/to/project --stage infer --model /path/to/DA3NESTED-GIANT-LARGE
python scripts/reconstruct_multiview.py --scene /path/to/source --output /path/to/project --stage fuse
```

`prepare` 固定输入划分、检查校准并准备图像；`infer` 仅处理训练图并记录模型与输入来源；`fuse` 输出观测网格、UV纹理、来源报告及 `world/scene.json`。在新项目目录执行；一旦已有 `world/scene.json`，各重建阶段均拒绝覆盖，重新重建须指定新的输出目录。

融合后的网格使用固定6倍TSDF体素的顶点聚类，并在全部训练相机上每16像素采样一次射线。任一视角的覆盖绝对下降超过3个百分点会保存 `simplification_audit.json` 并报错；覆盖只是稀疏诊断，不代表几何精度。贴图对大三角面做共享边一致的局部细分，将质心和边中点的源相机投影误差控制到0.5像素以内；最多8轮、100万面，未达要求会明确失败。

在工作台Python环境中构建并评价：

```bash
.venv/bin/python scripts/world.py --project /path/to/project build --expected-revision 1 --render
.venv/bin/python scripts/world.py --project /path/to/project evaluate --expected-revision 1 --build-id BUILD_ID
```

也可以用已有 `workspace_build_world`、`workspace_evaluate_world`、`workspace_publish_world` 工具完成构建、查看指标和发布；世界版本和显示场景版本分别读取。

## 外观、相机与评价

网格OBJ使用显式UV；`material.base_color_texture` 指向项目内PNG/JPEG。`appearance: captured` 保存照片中的观测光照，不能声称是已分解的固有颜色或可准确重光照材质。未提供纹理的原有材质接口继续可用。纹理会嵌入GLB，并随Blender工程保存；缺UV会明确报错。

精确视角由 `camera_to_world`、`intrinsics: {width,height,fx,fy,cx,cy}` 和 `reference_image` 定义。旧的look-at视角仍可用。网格、纹理、来源记录及参考图都与构建哈希绑定，修改后需要重新构建。

`visual_accuracy.status: measured` 表示已有图像测量，不代表质量通过。报告逐视角和全帧的RGB MAE、MSE、PSNR；透明区域合成白底并计入分母，不用有效像素掩盖缺面。alpha覆盖率是额外诊断，不是三维几何精度。数值在8位RGB编码域计算，没有拟合曝光或颜色；照片级真实、扫描几何精度与物理校准不能由这些数值单独证明。

纯读取的工具评价不写文件。需要比较图时，显式调用 `world_evaluation.write_view_inspection(project_dir, scene, build)`，生成实拍、重建、50%叠加和绝对误差四联图。查看留出结果后若再调模型，须将后续结果标为针对该验证集调优，不能继续称为未见过的测试结果。

算法依据：[DA3相机条件接口](https://github.com/ByteDance-Seed/Depth-Anything-3/blob/main/docs/API.md)、[Open3D RGB-D/TSDF融合](https://www.open3d.org/docs/release/tutorial/pipelines/rgbd_integration.html)。

## 首个真实场景结果

DL3DV卧室家具展厅（场景ID以 `05c8b23b4c3a` 开头）的309张原图中，固定48张训练图、6张留出图。最终首版约53万面，已有可编辑Blender工程、内嵌纹理GLB和同相机轨迹的独立重建/对比视频。留出图全帧RGB MAE为0.08459、PSNR为16.53 dB；这些结果不代表照片级真实或实测几何精度。

训练侧检查修复了过度QEM简化丢失地板、重复三角面及大三角UV插值偏移；最终采用上述固定聚类与投影细分。模型在留出评价前冻结，真实Codex完成构建、评价及隔离工作台发布，世界和来源文件保持不变。仍有远景、细杆缺面和纹理接缝，交付状态为 `needs_improvement`，不计入已完成高质量场景目录；原始图像没有可靠采集时间戳，10fps仅用于观看。

场景尚是一个整体观测网格，家具分割、米制尺度和物理校准未完成。完整输入划分、实际执行脚本快照、哈希、候选诊断和测量保存在spark工程中；大资产不进入代码仓库。
