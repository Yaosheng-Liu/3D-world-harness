# 统一轮次的重建实验

本目录只放实验代码与固定协议。原图、模型会话、密钥、Blender 工程和运行结果保存在计算端的独立实验根目录，不提交到 Git。

## 实验范围

三个 DL3DV 场景，每场景八次新运行：Astra/Sol 6.1 × 三种流程（基础工具单 Agent、专业工具单 Agent、完整 Harness），主实验使用 ultra；另补两种模型的 Harness/high。每次固定 M0 → R1 → M1 → R2 → M2；最后返修不追加第三次审核。单 Agent 两个修订节点自行检查。

输入任务为同一句简短提示词；公共输出规范由项目 AGENTS.md 提供。完整流程复用现有计划、分割、对象几何、场景构建与冻结审核实现，以无工作台的 CLI 入口执行，避免把 UI 会话历史带入实验。当前工具台不承担这批实验的调度。

## 目录与职责

| 文件 | 职责 |
|---|---|
| `protocol.json` | 模型、强度、轮次、任务、数据与预算配置 |
| `prepare.py` | 从原图建立 48 训练＋6 验证输入包和独立 12 视角评估窗口，重新初始化 DA3/TSDF |
| `run.py` | 新建上下文、计划／建模／审核、三检查点冻结及逐次 Token 记录 |
| `tools.py` | 所有组共用的 Blender 构建接口，专业组的分割、几何、深度、三角测量接口 |
| `evaluate.py` | 私有评估调度与 CSV/JSON 汇总 |
| `evaluate_blender.py` | 固定测试渲染、世界坐标几何导出、编辑与保存重开检查 |
| `image_metrics.py` | PSNR、SSIM、AlexNet LPIPS |
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

运行根目录一经创建不自动重跑失败试验；再次启动跳过已有目录。基础设施修复和实验失败均应保留记录，新的试验需建立新的协议目录。评价失败保存 error.json，修复后显式处理，不循环静默重试。`--model`、`--scene`、`--condition` 和 `--effort` 可限定运行队列。

监控使用已有 `*-coordinator.json`、`report-process.json` 和 `evaluation-process.json` 核对PID及命令。队列控制器退出且没有进行中的试验时，继续尚未启动的项目；有孤立调用则保留现场并在 `MONITOR.md` 标记待处理。后台统计和评测进程退出可恢复，模型失败与评测错误不自动重跑。全部试验结束且现有检查点均得到评价或错误记录后，写入 `automatic_completion.json` 并结束巡检；包含失败时明确记录 `finished_with_errors`，语义编辑核验仍单列。

当前协议首次建模上限60分钟，两次返修不设硬性时间上限，实际时间与Token分别记录。初始用户任务保持相同；公共输出说明告知时间策略，由控制器统一绑定固定输入来源并执行最终构建。此前超时记录保留，最后已报告的会话用量作为下界，不计为零。单次调用使用用户级systemd scope管理整个进程树，需Linux用户systemd管理器。建模Agent按需检查局部，整套逐物体移动、隐藏和保存重开由审核阶段统一执行。

v1因报告分组兼容、深度证据绑定及时间预算问题终止，产物和成本单独保留。v2从全新模型上下文开始，共用相同的训练观测缓存，不使用v1模型。`execution_gate.json`指定的首次完整验证须完成三版建模、两次审核及三版私有评测，才能放行其余队列；连续两次运行失败会写入`launch_hold.json`停止新启动，保留诊断现场。

本机可运行 `sync_records.py --remote spark145 --source EXP --destination LOCAL --interval 300`，持续镜像协议、进度、用量、指标和对象归属检查图。原始会话、认证及大型工程不进入同步清单；网络中断记入 `sync_status.json`，下一周期再次同步。两类巡检均使用文件锁防止重复启动。

## 方法边界与记录口径

- 所有组均提供 `cameras.json`，包含48张训练图和6张验证图的位姿及内参，图像路径对应校正后的输入。私有测试相机与图像不进入任务包。
- 工具组使用本批次从训练图新生成的中性 TSDF 观测几何，QEM 简化目标为30万面；算法可能无法达到目标，实际面数单独记录。不使用旧模型、自动补面或照片图集；保留简化前后训练覆盖诊断，几何仅作有误差的建模证据。对象分割与逐对象几何仍按计划或单 Agent 自己的请求执行。此初始化选择统一冻结，不根据测试结果选择算法。
- 身份和编辑目标在运行前登记。自动编辑指标检查申报对象的操作、非目标误改和保存重开；目标是否对应真实物体及其边界仍须独立视觉核验，未核验前明确标为 pending，不当成语义编辑成功率。
- Token 采用原始 CLI 日志口径。实测当前 CLI 续接会话报告累计用量，M1/M2 取相邻累计值差；缓存输入和推理输出为子项。缺字段和失败无完整用量时保留 unknown，不能记 0。
- DA3/TSDF 计算在每场景新算一次，为同场景工具条件共享。报告实际共享计算量，并另报每组冷启动计入该初始化的总成本。
- 原始源图像没有可靠时间戳。相机来自已有 SfM；几何参考来自独立留出图三视角三角化，是稀疏伪参考而非扫描真值。
- Agent 使用新上下文和 workspace-write 沙盒，读取限制由任务约定；没有声称操作系统级读隔离。每场景每组一次运行，只支持探索性比较。
