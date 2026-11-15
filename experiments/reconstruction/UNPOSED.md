# 无位姿 RGB 实验

协议设置 `input_mode: "unposed_rgb"`。输入是相同的 48 张训练 RGB 与 6 张可见验证 RGB；另留 12 张私有测试图。发布相机、扫描和测试图只能放在 `evaluation_private`，不挂入 DA3 推理容器，不复制到 Agent 项目。

- `single_basic` 没有初始化场景、相机或深度，只提供普通代码与 Blender。相机由它自行估计，按 `CAMERA_OUTPUT.md` 随每轮场景输出。
- `single_tools` 使用 RGB-only DA3 相机/深度及中性 TSDF，按需调用 SAM3、对象几何和测量工具，没有独立计划/审核。
- `harness_tools` 使用同一份工具证据，增加计划、对象初始化及两次独立审核返修。

三组都保存 M0/M1/M2；单 Agent 自行修订，只有 Harness 收到独立审核材料。模型、推理强度、外观规则、编辑目标与源码版本写入冻结协议。

## 数据准备

`prepare_unposed.py --root <batch> --stage rgb --source-root <mirror>` 生成纯 RGB 项目及隔离评测相机。`batch_spec.json` 中 `scenes` 将场景 key 映射到发布 ID；镜像的 `source_rgb/<id>/iphone` 存放按源帧命名的 JPG，私有扫描镜像保持发布目录结构。

随后在仅挂载 `shared`、`batch_spec.json`、源码与 DA3 权重的 GPU 容器中执行 `--stage infer --weights <weights>`，再执行 `--stage fuse`。54 张可见图参与联合相机估计，48 张训练图的深度用于融合。DA3 的深度与相机平移同步缩放，OpenCV 外参显式转换为右手 OpenGL c2w。

`scan_reference.py --private <evaluation_private/scene>` 在发布坐标下生成扫描首次命中参考及 RGB/扫描重投影图。正式启动前人工检查场景、相机方向与 RGB 对应。

## 执行与评测

沿用 `run.py` / `monitor.py` 的独立角色、冻结源码、两轮返修、用量及失败记录。每台机器按模型建立协议根，同一场景/模型的三种方法留在同一设备。支持 Docker 工具运行时，或通过 `tool_python` 指定具有相同工具依赖的原生 Python。`blender_gpu.py` 使用显式设备清单与文件锁选择 GPU；与 SAM3 共享 GPU0 锁可避免重叠占用。不配置 GPU 时 Blender 保留原 CPU 行为。

`evaluate.py` 根据协议调用无位姿评测：每个冻结版本用 48 张训练帧相机中心拟合一个正尺度、无镜像的全局 Sim(3)，将私有测试相机与扫描参考一起变换。禁止测试相机拟合、网格 ICP、逐物体对齐及缺失位姿真值补齐。相机缺失/轨迹退化写入评测错误，已建场景保留。

报告 RGB PSNR/SSIM/LPIPS、扫描可见表面的首次命中及最近表面误差、5/10cm 准确率、相机中心/旋转误差、编辑机械结果和失败覆盖。机械编辑成功不证明物体归属或遮挡补全；缺少实例标注时不报告实例真值指标。预处理成本按冻结缓存分别记录一次性及每次等价值，Token 与模型/工具/构建等待分别保存。

完整原视频缺失时应称“无位姿 RGB 关键帧重建”，不能声称完整原视频实验。公开扫描镜像、匿名化 mask 缺失等限制记录在评测来源中。
