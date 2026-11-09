# 统一轮次的重建实验

本目录只放实验代码与固定协议。原图、模型会话、密钥、Blender 工程和运行结果保存在计算端的独立实验根目录，不提交到 Git。

## 实验范围

2026-11-10补充两种外观规则的全新ultra实验：捕获外观允许照片光影；固有材质组禁止直接照片投影，但允许物体自身的干净纹理和显式灯光。每种规则18次、合计36次，所有Agent角色ultra。协议、启动方式和灯光/材质字段见[APPEARANCE.md](APPEARANCE.md)。原来的冻结纹理/灰模实验已按用户要求停止，历史结果保留。

三个 DL3DV 场景，每场景八次新运行：Astra/Sol 6.1 × 三种流程（基础工具单 Agent、专业工具单 Agent、完整 Harness），主实验使用 ultra；另补两种模型的 Harness/high。每次固定 M0 → R1 → M1 → R2 → M2；最后返修不追加第三次审核。单 Agent 两个修订节点自行检查。

输入任务为同一句简短提示词；公共输出规范由项目 AGENTS.md 提供。单Agent修订只获得原始输入、上一版场景和简短续修指令，不生成或提供系统审核四联图、叠图、RGB误差图或独立审核反馈。普通Blender预览仍可使用。上述审核材料由完整Harness的审核阶段生成、读取并反馈。完整流程复用现有计划、分割、对象几何、场景构建与冻结审核实现，以无工作台的 CLI 入口执行，避免把 UI 会话历史带入实验。

## 目录与职责

| 文件 | 职责 |
|---|---|
| `protocol.json` | 模型、强度、轮次、任务、数据与预算配置 |
| `prepare.py` | 从原图建立 48 训练＋6 验证输入包和独立 12 视角评估窗口，重新初始化 DA3/TSDF |
| `continue_v1.py` | 复用v1首版；隔离单Agent旧审核材料和会话，多Agent组保留原断点 |
| `run.py` | 新建上下文、计划／建模／审核、三检查点冻结及逐次 Token 记录 |
| `tools.py` | 所有组共用的 Blender 构建接口，专业组的分割、几何、深度、三角测量接口 |
| `evaluate.py` | 私有评估调度与 CSV/JSON 汇总 |
| `evaluate_blender.py` | 固定测试渲染、世界坐标几何导出、编辑与保存重开检查 |
| `image_metrics.py` | PSNR、SSIM、AlexNet LPIPS |
| `texture_ablation.py` / `texture_ablation_blender.py` | 冻结模型的已有纹理／统一灰模配对渲染 |
| `report.py` | 只读队列进度与各角色实时累计 Token 摘要 |
| `monitor.py` | 每5分钟核对队列与评测进度，恢复退出的后台服务，保留失败和孤立调用 |
| `sync_records.py` | 将允许清单内的统计和检查图同步到本机，不复制认证或会话 |
| `common.py` | 原子记录、哈希、CLI 用量快照与运行矩阵 |

计算端输出结构：

```text
experiment-root/
  protocol.json, runtime.json
  shared/<scene>/{source,project,prepared.json,initialization.json}
  evaluation_private/<scene>/   # 不进入 Agent 输入包
  runs/<scene>__<condition>__<model>__<effort>/
    spec.json, status.json
    homes/                     # 本次独立认证与会话；严禁同步或发布
    project/                   # 输入、候选与审核证据
    calls/                     # 每次原始事件、提示词、Token、时间
    checkpoints/{M0,M1,M2}/    # 冻结场景、工程、源文件与哈希
  evaluation_results/<trial>/<checkpoint>/
  results.csv, results.json
```

## 执行

```bash
python3 experiments/reconstruction/prepare.py --root EXP --runtime RUNTIME --stage inputs
python3 experiments/reconstruction/prepare.py --root EXP --runtime RUNTIME --stage initialize
python3 experiments/reconstruction/run.py --root EXP --runtime RUNTIME
python3 experiments/reconstruction/evaluate.py --root EXP --runtime RUNTIME
python3 experiments/reconstruction/monitor.py --root EXP --runtime RUNTIME --interval 300
```

对已完成的M2补充外观消融，无需重新调用Agent：

```bash
python3 experiments/reconstruction/texture_ablation.py --root EXP --runtime RUNTIME --output ABLATION --workers 2
```

两套渲染共用冻结几何和12个测试相机，统一为粗糙度0.8、金属度0的受光材质，以及固定参数的相机随动诊断灯、环境光、Standard色彩映射和32次Cycles采样。`textured`保留已有纹理或原常量颜色；`clay`替换为统一线性RGB 0.5，不保留纹理或原对象颜色。原本没有纹理的对象不会补造贴图，覆盖数量写入结果。这是捕获外观与中性灰模的诊断对照，不是精确分离纹理频率的实验，也不声称恢复了真实照明。灰模对原RGB的PSNR/SSIM/LPIPS仅作为诊断，不能作为几何质量指标；几何指标复用同一冻结版本原有测量。

消融只写独立目录，检查输入工程SHA、几何不变与相机投影，保留逐视角图像、指标及统计。`--trial`可先验证一组，后续省略该参数时复用已完成结果；`--checkpoint`可显式选择M0或M1。私有视角和诊断结果不进入Agent输入包。

运行根目录一经创建不自动重跑失败试验；再次启动跳过已有目录。基础设施修复和实验失败均应保留记录，新的试验需建立新的协议目录。评价失败保存 error.json，修复后显式处理，不循环静默重试。`--model`、`--scene`、`--condition` 和 `--effort` 可限定运行队列。

按用户选择续跑v1时，先运行`continue_v1.py --source V1 --root CONTINUATION`。原始日志、冻结成果与历史预算留在v1；18个已验证首版以硬链接复用。单Agent从干净工程和新返修会话开始，旧已暴露审核材料的返修排除并单列诊断消耗；完整Harness复用有效审核与原会话断点，不重跑首版。未完成首版的实验单独补齐。续跑批次是带执行历史的探索记录，不宣称全部样本具有同一初始时间预算。

提交检查允许报告更正跨视角关联、增加计划外排除说明；原始计划和观测保持不变，报告引用仍须指向存在的场景对象和训练帧。视觉重建允许空碰撞体，物理入口仍要求有效碰撞几何。审核提示及标准不随此改动加严。明确恢复的试验保留原失败状态到`resume_history`，通过`validation_recovery.json`记录采用的策略，结果表`submission_policy`标明该试验经过放宽检查的续跑，不与原始失败口径混淆。若完成的建模调用已有候选但尚未冻结，`resume.json`可登记`saved_submission`的检查点、原调用目录和候选SHA；控制器验证后直接构建并进入既定审核，不重复调用建模Agent。

离线构建通过`build_glb_limit_bytes`显式配置GLB预算，当前实验为256MiB；默认工作台导入仍为100MiB。较大的纹理工程继续保留在计算端，恢复构建不改变模型几何或压缩原始纹理，仍校验容器、物体身份及资源一致性。

已完成的[20261109续跑报告](reports/20261109_v1_continue/README.md)包含24个条件、72版自动指标、三场景均值、Token及耗时和论文实验片段。报告保留负面结果并披露执行期间的协议修订；语义编辑归属仍待独立视觉核验。

Token按模型线程最后累计快照去重，叠加独立规划／审核调用；单Agent继承首版和新返修线程分别计入。历史中断和当前调用实际耗时均保留，排除的旧反馈尝试见`excluded_feedback_attempts.json`。私有测试及对象归属检查图不进入任何建模输入。

监控使用已有 `*-coordinator.json`、`report-process.json` 和 `evaluation-process.json` 核对PID及命令。队列控制器退出且没有进行中的试验时，继续尚未启动的项目；有孤立调用则保留现场并在 `MONITOR.md` 标记待处理。后台统计和评测进程退出可恢复，模型失败与评测错误不自动重跑。全部试验结束且现有检查点均得到评价或错误记录后，写入 `automatic_completion.json` 并结束巡检；包含失败时明确记录 `finished_with_errors`，语义编辑核验仍单列。

当前协议首次建模上限60分钟，两次返修和独立审核均不设硬性时间上限，实际时间与Token分别记录。初始用户任务保持相同；公共输出说明告知时间策略，由控制器统一绑定固定输入来源并执行最终构建。此前超时记录保留，最后已报告的会话用量作为下界，不计为零。单次调用使用用户级systemd scope管理整个进程树，需Linux用户systemd管理器。建模Agent按需检查局部，整套逐物体移动、隐藏和保存重开由审核阶段统一执行。审核中断后优先校验并复用同一候选的冻结检查材料，新调用保存在单独attempt目录；续接使用当前最新模型会话，不增加反馈轮数。

v1因报告分组兼容、深度证据绑定及时间预算问题终止，产物和成本单独保留。v2从全新模型上下文开始，共用相同的训练观测缓存，不使用v1模型。`execution_gate.json`指定的首次完整验证须完成三版建模、两次审核及三版私有评测，才能放行其余队列；连续两次运行失败会写入`launch_hold.json`停止新启动，保留诊断现场。

本机可运行 `sync_records.py --remote spark145 --source EXP --destination LOCAL --interval 300`，持续镜像协议、进度、用量、指标和对象归属检查图。原始会话、认证及大型工程不进入同步清单；网络中断记入 `sync_status.json`，下一周期再次同步。两类巡检均使用文件锁防止重复启动。

需要增加并行时，在运行根目录的`scheduler.json`设置`workers_per_model`及`before_gate_trials`。每个槽位独立运行一个指定条件，兼容已运行的两个队列进程；完整链门槛通过前只调度登记的验证试验，通过后自动补位。该配置属于运行调度，不改变协议、模型或返修轮数。并行更改与资源检查保存在`parallelism_history.json`。

## 方法边界与记录口径

- 所有组均提供 `cameras.json`，包含48张训练图和6张验证图的位姿及内参，图像路径对应校正后的输入。私有测试相机与图像不进入任务包。
- 工具组使用本批次从训练图新生成的中性 TSDF 观测几何，QEM 简化目标为30万面；算法可能无法达到目标，实际面数单独记录。不使用旧模型、自动补面或照片图集；保留简化前后训练覆盖诊断，几何仅作有误差的建模证据。对象分割与逐对象几何仍按计划或单 Agent 自己的请求执行。此初始化选择统一冻结，不根据测试结果选择算法。
- 身份和编辑目标在运行前登记。自动编辑指标检查申报对象的操作、非目标误改和保存重开；目标是否对应真实物体及其边界仍须独立视觉核验，未核验前明确标为 pending，不当成语义编辑成功率。
- Token 采用原始 CLI 日志口径。实测当前 CLI 续接会话报告累计用量，M1/M2 取相邻累计值差；缓存输入和推理输出为子项。缺字段和失败无完整用量时保留 unknown，不能记 0。
- DA3/TSDF 计算在每场景新算一次，为同场景工具条件共享。报告实际共享计算量，并另报每组冷启动计入该初始化的总成本。
- 原始源图像没有可靠时间戳。相机来自已有 SfM；几何参考来自独立留出图三视角三角化，是稀疏伪参考而非扫描真值。
- Agent 使用新上下文和 workspace-write 沙盒，读取限制由任务约定；没有声称操作系统级读隔离。每场景每组一次运行，只支持探索性比较。
