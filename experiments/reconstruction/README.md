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
```

运行根目录一经创建不自动重跑失败试验；再次启动跳过已有目录。基础设施修复和实验失败均应保留记录，新的试验需建立新的协议目录。评价失败保存 error.json，修复后显式处理，不循环静默重试。`--model`、`--scene`、`--condition` 和 `--effort` 可限定运行队列。

## 方法边界与记录口径

- 工具组使用本批次从训练图新生成的中性 TSDF 观测几何，统一用 QEM 限制为最多 30 万面，不使用旧模型、自动补面或照片图集；保留简化前后训练覆盖诊断，几何仅作有误差的建模证据。对象分割与逐对象几何仍按计划或单 Agent 自己的请求执行。此初始化选择统一冻结，不根据测试结果选择算法。
- 身份和编辑目标在运行前登记。自动编辑指标检查申报对象的操作、非目标误改和保存重开；目标是否对应真实物体及其边界仍须独立视觉核验，未核验前明确标为 pending，不当成语义编辑成功率。
- Token 采用原始 CLI 日志口径。实测当前 CLI 续接会话报告累计用量，M1/M2 取相邻累计值差；缓存输入和推理输出为子项。缺字段和失败无完整用量时保留 unknown，不能记 0。
- DA3/TSDF 计算在每场景新算一次，为同场景工具条件共享。报告实际共享计算量，并另报每组冷启动计入该初始化的总成本。
- 原始源图像没有可靠时间戳。相机来自已有 SfM；几何参考来自独立留出图三视角三角化，是稀疏伪参考而非扫描真值。
- Agent 使用新上下文和 workspace-write 沙盒，读取限制由任务约定；没有声称操作系统级读隔离。每场景每组一次运行，只支持探索性比较。
