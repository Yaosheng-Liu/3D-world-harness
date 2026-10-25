# 真实多视角重建

本轮提供一条**已知相机参数**的重建流程：真实训练图像 → 相机条件深度 → 多视角一致性过滤 → TSDF 网格 → 训练图像纹理 → Blender/GLB → 留出视角图像评价。输入相机来自已有 SfM；本脚本尚不为无相机的照片集合求解位姿。

## 输入与尺度

输入目录包含 Nerfstudio 格式 `transforms.json` 和其中引用的图像。当前支持全序列共享内参的 PINHOLE/OPENCV 相机；逐帧内参、鱼眼和高阶畸变会明确拒绝。相机矩阵采用 OpenGL camera-to-world；焦距、主点和图像宽高必须对应实际图像，不重复应用已有坐标变换或去畸变。脚本用训练图像的特征匹配比较保留原图与应用所记录畸变两种投影，记录选择及残差。

默认先固定48个训练视角、6个留出视角。留出RGB不进入深度推理、融合或纹理；相机可能由包含所有图像的SfM得到，因此这不是独立相机真值验证。实际观察过的候选图像另记来源。

未标定尺度保存为 `units: arbitrary`。网格、相机和观测使用相同坐标，并记录 `source_to_world`；MuJoCo拒绝此类世界，不能把一个数据单位默认为一米。初始化后的物体清单、SAM 3分割、逐物体几何及建模审核由[物体重建流程](object-reconstruction.md)承接；未观测背面仍需明确标注为估计补全，质量/摩擦标定暂缓。

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

## 2026-10-24 阶段复核与后续流程

最初目标是外观接近实拍、对象能独立编辑、可校准物理行为的场景。当前已完成可运行的重建与检查工具，但尚未达到这个目标。

spark卧室第二版确实执行了Blender编辑：分离`main_bed`的17320个面、补23个小面、恢复680个常量UV面的照片投影，添加2082个局部地面三角面；当前还有`observed_environment`和`floor_repairs`。主床仍缺背面、底面及部分床脚，其他家具未逐一分离。留出PSNR仅从16.5281增至16.5608dB，报告仍是`needs_improvement`、`accepted: false`。不能把资产分割、脚本成功或全部视角像素误差略降当作完成真实建模。

本次还发现，发布GLB并不会自动绑定参考片段：18779会话的`reference_clip`原先为空。现已导入项目`delivery/original.mp4`及`workbench_reference/cameras.json`，309帧、10fps、30.9秒；相机由源坐标转换为GLB的Y-up坐标。真实浏览器验证首/中/末时间跳转、全部帧相机和同机位Cycles显示。该视频是原始图序列的观看副本，10fps不是原生采集时间戳；正式测量继续使用原始图像。

### 建议实施顺序（尚未实现自动双Agent审核）

1. **固定输入与证据**：每个任务明确源帧、相机、可用尺度证据和坐标；生成物体清单及跨视角对应。参考视频必须随场景初始化绑定，并验证至少首/中/末帧。审核图关闭参考叠加，单独保留叠图诊断。
2. **先完成局部可编辑资产**：用现有融合网格作观测依据，优先完成主床、两侧床头柜和邻近墙地面。床框、床头、床垫、枕被分别保留可编辑结构；修复支持关系、轮廓、缺失床脚和纹理边界。观测不足的背面补全明确标为估计，不能称实测；视觉布料不强制闭合，碰撞体另建。移动主床时不得带走柜子/墙面，也不能残留重复床体。
3. **以两种角色迭代**：建模Codex修改版本化资产并提交候选；独立审核Codex读取冻结候选、原始证据、规定视角与自动检查结果，只写审核报告和问题单，不修改模型，也不替建模Agent发布。复用已有world build/evaluate/publish，不为每个Blender操作增加包装工具。
4. **由harness执行审核门槛**：物体ID、层级、变换、碰撞/视觉对应和资产哈希由程序检查；审核Agent检查轮廓、物体完整性、遮挡/支撑关系、纹理接缝以及未观测区域的合理性。每个问题包含候选哈希、object_id、帧/视角、图中区域、严重程度、证据和可复验标准。建模Agent收到前几项阻塞问题后返工，下一轮审核必须复验旧问题及回归视角。
5. **再分离材质并校准物理**：结构通过后再处理纹理一致性、反照率/光照分离和PBR外观；米制尺度与重力方向确认后，建立独立碰撞代理，验证接触、支撑与编辑后的运动。照片不能给出实测质量和摩擦；缺少实测信息时参数保持估计标签。

验收必须分开记录外观、结构完整性、对象可编辑性、物理标定。全帧MAE/PSNR保留为诊断，同时看对象区域、轮廓和最差视角，不能以“每个训练视角像素误差都不增”作为唯一选择规则。照片光照匹配和新视角结构检查应分开，避免为了原图得分永久保留错误表面。

最终验收视角不进入反复的修改反馈；现有6张图已被多轮查看，只能继续作为回归/验证集，不能重新称盲测。新视角绕物体渲染能暴露结构缺陷，但没有对应实拍时不证明真实几何。两个相同模型的Agent也可能共享盲点，仍需确定性检查和必要的人类复核。建议每个对象先限制3轮返工；无实质改善、证据不足或相互矛盾时记录阻塞原因，升级方法或补采素材，不能无限自我评价并自动宣告通过。候选可以进入工具台供审查，只有全部必需验收项通过才进入已完成场景目录。

## 建模与独立审核闭环

`scripts/review_reconstruction.py` 复用工具台的建模会话，将提交候选、确定性检查、独立只读审核、问题回传和通过后发布串起来。建模固定为 `gpt-6-astra / high`；审核默认 `gpt-6.1-sol / ultra`，模型不可用时明确失败，不自动降级。spark旧CLI 0.156.1曾拒绝Sol 6.1，经用户允许临时配置过Sol 6 Ultra；独立安装CLI 0.164.0后，Sol 6.1 Ultra实际请求成功，已恢复原指定模型。建模工作台仍使用原运行环境；状态同时保留请求与实际模型。本阶段物理不作为验收项，也不运行仿真。

先通过工具台向Astra提交建模任务，要求新资产使用新目录、输出完整world候选JSON以及`build_world(scene=candidate, render=True)`的receipt；候选版本为当前世界版本加一。建模者保留当前`world/scene.json`与已发布场景。然后在同一计算端运行：

```bash
.venv/bin/python scripts/review_reconstruction.py \
  --project /path/to/project --url http://127.0.0.1:18779 \
  --candidate refinement_v3/candidate.json \
  --receipt refinement_v3/build_receipt.json \
  --feedback-id BUILDER_FEEDBACK_ID \
  --codex /path/to/codex --blender /usr/bin/blender
```

控制器等待对应反馈完成，校验构建与全部依赖哈希，冻结场景和证据目录；独立重开Blender，逐物体平移、旋转，验证父子对象随动、其他网格不动及原点位置，并生成编辑前后图。它生成同相机的原图、独立渲染、叠图与误差图。审核Codex以单独会话、`read-only`沙盒、禁止提权审批运行，结构化结果由CLI写出；其任务不修改模型和发布场景。审核前后的源资产与证据均校验哈希。

接受结果必须绑定候选ID，覆盖全部固定视角及对象，外观、结构、编辑性均通过，程序检查通过，且无阻塞/主要问题。旧问题必须保留原ID或明确列为解决。未通过时，前几项问题连同完整报告回传同一个Astra会话，在新目录返工；最多3轮，仍不通过或证据不足时保留问题并停止自动迭代，不伪造通过结果。失败不会自动重发不确定的模型请求。审核通过才将候选作为新世界版本提交并发布；已完成场景归档仍需另外完成真实场景媒体与交付验证。

工具台场景上方的「重建流程 / 物体编辑」展示当前显示版本、候选、轮次、两个角色、检查和问题单。候选原图/独立渲染可直接查看；问题定位到审核视角，可返回主工具台对齐同机位。`reconstruction/status.json`保存进度，各轮证据位于独立目录，未通过候选不会替换当前显示版本。

需要本轮审核结束后停止时，启动控制器时传 `--max-rounds 1`；保留候选和问题单，下次再修复，不自动提交下一轮建模。本次按用户收尾要求使用此设置。

spark的独立审核CLI位于 `/home/yao/3D-harness/runtime/review-codex-0.164.0/`，使用其 `home` 子目录作为 `CODEX_HOME`。Ubuntu 24.04最初阻止bubblewrap创建用户命名空间；经用户明确授权，加载Ubuntu软件包提供的 `/etc/apparmor.d/bwrap-userns-restrict` 配置，未关闭全局AppArmor限制。实测只读沙盒可读工程，写文件和访问本地工具台端口均被拒绝；Sol 6.1 Ultra随后实际读取世界版本并识别附图。安装与探测记录留在该运行目录。环境探测成功不代表场景质量通过。配置依据：[官方Linux沙盒说明](https://learn.chatgpt.com/docs/sandboxing)。

物体编辑针对当前已发布世界：选择物体、定位、修改父对象坐标下的位置或XYZ旋转，构建成功后再提交与发布；新GLB和Cycles使用同一工程。Agent运行、排队或重建审核期间禁止直接编辑。编辑会保留世界历史，并把新版本标为待复核，不继承旧版本的质量通过结论。此入口修改变换；几何、拓扑、纹理和父子结构由建模Agent或Blender编辑。

验证：审核/编辑门槛10项、world48项、Cycles7项、插件11项通过。`scripts/test_reconstruction_workflow.py`在独立合成项目运行真实Blender与Chromium，验证候选图、问题区域、通过后发布、逐对象变换、保存后邻居不变、Cycles同步及手机布局；该合成检查不代表真实卧室候选通过。运行此检查需要Playwright及浏览器、NumPy、Pillow、Blender，且不会调用模型。

### 本次收尾位置

正式18779服务已在Astra自然完成后更新；真实浏览器验证309帧参考、候选图、精确机位对齐、Cycles和手机布局，无页面错误。展厅V3候选64对象保存在spark工程 `multiview-bedroom-20261023-surface-preserved/refinement_v3/`，正式审核目录为 `reconstruction/20261023T195444Z-1ad79aaa/round_01/`。固定六视角PSNR约18.07dB只是诊断；4个抽屉原点未通过独立程序检查，墙地面还残留捕获阴影，外围仍有碎面，不能称完整真实还原。

本机中间成果在 `/home/yao/proxy2real/exports/reconstructed_scenes_intermediate/dl3dv_05c8_v3_20261024/`。`review_comparison.mp4`是8个训练视角的原图/V2/V3对比幻灯视频；`main_bed_turntable.mp4`是主床48帧新机位环绕渲染。两段均8秒，已完整解码验证，均不是整场景连续重建视频。候选未加入已完成场景清单；下次从该候选和独立问题单继续，物理仍暂缓。
