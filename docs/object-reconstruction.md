# 物体计划、SAM 3 分割与实例几何

这条流程使用已知相机的多视角原图，把物体身份贯穿计划、分割、几何证据、Blender 建模和独立审核。物理与未知相机估计不在当前阶段。物体几何输出是未补全的观测表面；`ready` 只表示可以交给建模 Agent，不代表场景审核通过。

![本轮 harness 框架](assets/reconstruction-harness.png)

## 运行完整流程

入口为 `scripts/run_reconstruction.py`，前置输入沿用[已知相机多视角流程](multiview-reconstruction.md)的 prepared 原图、DA3 深度与已初始化世界。先复制并填写 [runtime.example.json](../examples/object_pipeline/runtime.example.json) 中本机的执行程序和权重路径。计划和审核分别运行只读 Codex 会话；建模复用工具台当前 Astra High 会话。运行环境可为角色指定 `home`，控制器只在调用该角色时设置 `CODEX_HOME`。

```bash
python scripts/run_reconstruction.py \
  --project /path/to/project --runtime /path/to/runtime.json \
  --url http://127.0.0.1:18779 --max-rounds 3
```

`--stop-after plan|segment|geometry` 可在对应证据阶段结束后停下，不启动后续建模。默认 `review` 运行完整闭环。`--max-rounds` 限制本次调用的建模/审核轮数为 1–3；每个需要重规划的轮次最多尝试两版计划及对应分割/几何。证据仍有阻塞时保存 `needs_revision`，不会把漏检当成不存在。

```bash
python scripts/run_reconstruction.py \
  --project /path/to/project --runtime /path/to/runtime.json \
  --url http://127.0.0.1:18779 --resume RUN_ID --max-rounds 1
```

显式续跑使用原运行 ID，在新的证据轮次继续，保留全部物体身份、排除依据和旧审核问题。若已提交的建模任务尚未审核，则等待同一反馈并审核已有候选，不重新发送建模任务；响应丢失时通过保存的请求键查找已提交反馈，查不到则明确停止。已发布世界发生变化时拒绝继续旧运行。已有证据在复用前重新检查来源、对象覆盖和文件哈希。

运行状态位于 `object_pipeline/status.json` 与 `object_pipeline/<run_id>/status.json`，每轮保留计划、分割、几何、建模提示与候选。旧审核仍保存在 `reconstruction/`；状态同时记录关联审核运行和报告。工具台的“重建流程 / 物体编辑”显示当前计划及分割证据、待解决事项、独立审核和已发布对象，三者分别展示。

审核问题的 `target` 决定下一轮：`planning` 和 `segmentation` 先退回计划 Agent，根据原图、漏检与几何冲突重规划；`modeling` 复用已经验证的物体证据交给建模 Agent。新发现的清单外物体可用 `object_id=scene` 和 `source_<frame_index>` 原图区域报告。旧问题必须保持 ID 或明确解决。只有审核通过且确定性检查通过，才调用现有接受/发布入口。普通手动发布接口仍是独立路径，不能据此声称所有发布都经过新流程。

计划流程的候选必须完整保留当前世界的固定验证相机、内参和原图路径，可增加检查视角，不能删除或改绑原有视角。独立重开 Blender 后，逐对象进行变换检查，并生成隐藏自身及后代前后的图像与接触表。相机按投影包围盒选择，遮挡可能仍使检查不充分，审核 Agent 必须指出并要求补机位。`edit_visual_coverage` 只证明每个应检对象有检查图片，物体分离正确性由原图和逐对象图片共同审核。

runtime 中的工具命令是 JSON 参数数组，支持 `{repo}`、`{project}`、`{run_dir}`、`{run_relative}`、`{plan}`、`{segments}`、`{output}`；控制器不执行 shell 字符串。可用它配置隔离容器，项目输入只读，只有本次运行目录可写。Spark 的实际配置在 `/home/yao/3D-harness/runtime/object-pipeline.json`，复用已有 SAM 3 和 DA3/Open3D 镜像。接口不自动下载权重或切换分割/审核模型。

```mermaid
flowchart TD
    RGB[原图与已知相机] --> PLAN[计划 Agent：物体清单、层级、参考视角]
    RGB --> DEPTH[DA3：共享坐标系深度]
    PLAN --> SAM[SAM 3：文字检测与点框修正]
    SAM --> CHECK[逐项检查漏检与跨视角身份]
    CHECK -->|漏检、串 ID、遗漏区域| PLAN
    CHECK --> GEO[按物体融合观测几何与受掩码约束的纹理]
    DEPTH --> GEO
    PLAN -->|明确选择按参考建模| MODEL[建模 Agent：形状补全、材质、父子关系]
    GEO --> MODEL
    MODEL --> REVIEW[独立审核：原图对照、隐藏与移动检查]
    RGB --> REVIEW
    REVIEW -->|遗漏或拆分错误| PLAN
    REVIEW -->|形状或材质问题| MODEL
    REVIEW -->|通过| PUBLISH[发布可编辑场景]
```

## 计划与文件约定

所有操作使用独立运行目录。下面以工程内的 `reconstruction/run_01/` 为例；路径全部相对于当前工程，不接受逃逸路径。原始图片、相机、DA3 深度和旧运行不被覆写。

`plan.json` 包含 `schema_version=1`、`summary`、`source_manifest_sha256`、`objects`、`unexplained_regions` 与 `excluded_objects`。`source_manifest_sha256` 绑定 `observations/manifest.json`。每个物体包括：

- `id`：稳定且唯一；更改名称不改变身份。
- `name`、`category`、`parent_id`、`kind`（`group`、`object`、`part`、`structure`）。
- `priority`、`geometry_strategy`（`fuse`、`model`、`group`）、`segmentation_prompt`。
- `observations`：原始 `frame_index`、归一化 `[x0,y0,x1,y1]` 框和证据描述。只使用冻结的训练图像；留出图像不得进入计划、分割或几何融合。
- `uncertainty`、`acceptance_criteria`：缺失证据和可执行验收条件。

计划不能凭 SAM 未检出就删除对象。新的计划可修正视角、框或物体层级；明确排除的对象须进入 `excluded_objects` 并保留理由与证据帧。选择 `geometry_strategy=model` 表示 Agent 明确根据原图直接建模；它不会自动将分割失败伪装成成功融合。

## SAM 3 分割

在安装了官方 SAM 3 及其权重的环境中运行：

```bash
python scripts/sam3_segment.py \
  --project /path/to/project \
  --plan reconstruction/run_01/plan.json \
  --output reconstruction/run_01/segmentation \
  --checkpoint /path/to/sam3.pt
```

`--output` 必须是新目录。适配器先尝试文字提示，按计划框匹配实例；失败后用 SAM 3 的交互框提示修正。它不会自动切换 SAM 2。输出为：

- `segments.json`：绑定计划与原图清单哈希；记录模型版本、权重哈希、阈值以及每次尝试。
- 每对象 `status`：`segmented`、`missing`、`uncertain`、`not_required`。没有检出的对象仍留在清单里。
- 每有效观察：`frame_index`、`mask_path`、`mask_sha256`、`score`、`prompt_method`、`overlay_path`。掩码是与对应 prepared 原图同尺寸的 0/255 PNG。

当前适配器对选定图片独立推理，尚未使用 SAM 3 视频传播。`segmented` 只说明取得二维掩码；模型分数、框重合率均不代表正确身份的概率。相邻同类物体、遮挡部件和语义边界仍需几何与原图检查。

Spark 已有隔离运行方式（不需重新下载权重）：

```bash
docker run --rm --gpus all --network none --user "$(id -u):$(id -g)" \
  -v /home/admin/workspace/projects/sam3:/workspace/projects/sam3:ro \
  -v /home/admin/workspace/models/sam3:/workspace/models/sam3:ro \
  -v /home/yao/3D-harness/3D-world-harness:/repo:ro \
  -v /path/to/project:/project:ro \
  -v /path/to/project/reconstruction/run_01:/project/reconstruction/run_01 \
  --entrypoint python sam3d-objects:dgx-seg /repo/scripts/sam3_segment.py \
  --project /project --plan reconstruction/run_01/plan.json \
  --output reconstruction/run_01/segmentation \
  --checkpoint /workspace/models/sam3/sam3.pt
```

## 逐物体几何证据

在已有 DA3/Open3D 环境中运行，不需要再次载入 DA3 模型：

```bash
python scripts/reconstruct_objects.py \
  --project /path/to/project \
  --plan reconstruction/run_01/plan.json \
  --segments reconstruction/run_01/segmentation/segments.json \
  --output reconstruction/run_01/geometry
```

该步骤复用 `dense/depths.npz` 的深度、置信度、相机和处理后 RGB，并检查其 `inference.json` 哈希绑定。工程的 `split_manifest.json` 所指原数据目录仍需可读。Spark 的 `mv-sam3d:gb10-da3` 镜像已具备 Open3D；该步骤使用 CPU，无需 GPU。容器挂载需保留工程与原数据的绝对路径。

处理规则：

1. 通过原图内参与深度内参之间的变换映射掩码，兼容预处理缩放与裁剪。
2. 子部件拥有已标注的像素；祖先掩码扣除这些像素。无父子关系的重叠像素从双方剔除并报告冲突。
3. 边界收缩一个深度像素。每个保留深度点需获得其他视角同一 ID、相近深度的支持；可见冲突不能与支持打平。前景遮挡不算身份冲突。
4. 每个对象单独 TSDF 融合；未观测处保持开口，不自动补全。
5. 每个对象的贴图只包含其掩码内像素，其他位置为中性灰。面纹理来源还需通过深度与可见性检查；投影 UV 插值误差按既有方法限制。

输出 `geometry.json` 保留全部计划对象，状态为 `ready`、`insufficient_evidence`、`needs_revision` 或 `not_required`。有观测表面的对象生成独立 `observed.obj`、`observed.mtl`、`captured.png`；坐标与尺度保持原数据坐标。达到明确的 UV 面数或细分迭代上限时，该对象记为 `needs_revision` 并记录具体原因，其余对象继续；不自动降低质量，计划 Agent 可调整拆分或明确选择参考建模。其他执行错误仍中断并记录，不能冒充缺少观测。失败后显式续跑可复用已完成的计划和分割，仅重跑几何到新目录。

`objects[].files` 与顶层 `files` 是工程相对路径到 SHA-256 的映射，包含全部几何输出；`input_files` 绑定计划、分割、掩码、深度和校准文件。审核应验证这些哈希，不必复制大型深度文件。

## 限制与验证

2026-10-25 在正式 Spark 工程运行了真实 Astra High 计划与一次证据修订：计划从72项增加为78项，保留旧 ID；SAM 3 为77个非分组项生成二维掩码。最终逐物体报告为23项观测几何就绪、4项证据不足、1项 UV 面数超限待改进、50项明确按参考建模或分组；仍有8个未解释区域，流程保留 `needs_revision`，未启动本轮建模或替换已发布世界。几何续跑复用原计划和掩码，未重复调用模型。运行记录为 `object_pipeline/20261025T051434Z-0ee3dce8/`。

新审核入口另在独立合成工程实际调用 `gpt-6.1-sol / ultra`，读取两张原图、两个候选渲染和逐物体隐藏检查，109.62秒后返回 `revise`，识别到预设尺寸偏差并路由到 `modeling`；schema和冻结哈希校验通过，未发布。这证明真实角色接口和证据交接可用，不证明家具展厅已经还原。正式工具台的计划/证据检查与独立合成工程的旧发布、对象编辑、Cycles浏览器回归均通过。

跨视角检查可以发现部分串 ID、错边界和深度冲突，不能证明语义判断一定正确。只出现在一个视角的对象通常无法获得融合支持，但仍可以经计划明确选择按参考建模。缺失子部件的掩码不能自动确定其在祖先表面中的全部归属；审核还须进行隐藏、移动和残留纹理检查。照片纹理保留原光照、阴影和反光，尚未完成材质与光照分离。

2026-10-25 的真实联调复用了 Spark `multiview-bedroom-20261023-surface-preserved` 工程的原图第 1、7 帧：SAM 3 为台灯和椅子各得到两帧掩码；两者生成独立观测网格。故意构造的漏检提示仍以 `missing` / `insufficient_evidence` 留存。这是组件联调，不是完成或发布场景。

测试命令：

```bash
python -m unittest discover -s backend/tests -p test_sam3_segment.py -v
python -m unittest discover -s backend/tests -p test_reconstruct_objects.py -v
```

几何测试覆盖实际 TSDF、相机对齐、遮挡、错实例、父子所有权、邻居纹理隔离、漏检保留及输入哈希/留出图像检查。在缺少 Open3D 的环境中，两项融合测试显式跳过，不能把这种运行描述为融合验证通过。
