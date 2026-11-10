---
name: worldharness-experiments
description: 安装并复现 WorldHarness 的 DL3DV 十场景重建主实验，配置跨设备运行环境，执行单 Agent、单 Agent 加工具和完整 Harness，记录冻结版本、指标及 Token。用于协作者实验交接和分工运行。
---

# WorldHarness 十场景实验

目标是从相同多视角输入生成可逐物体编辑的场景程序，并比较三种流程。使用本 Skill 附带的第三版十场景清单；不要重新挑场景或读取旧重建作为输入。

## 先明确这次分工

确定参与者负责的场景 key、模型、输出目录和设备。已经给出的分工直接执行；没有给出时先完成安装、数据校验和配置，启动付费模型运行前确定负责范围。不要让多人写同一个实验根目录。

每个实验根目录只配置一种模型。三个主模型为 `gpt-6-luna`、`gpt-6.1-sol`、`gpt-6-astra`，Astra/Sol 使用 `ultra`，Luna 使用 `xhigh`；完整 Harness 的计划、建模、审核使用该根目录的同一模型和等级。模型不可用时记录并停止该模型，不替换名称或推理等级。

## 按需阅读

1. 新机器：读 [安装与设备适配](references/setup.md)，从 GitHub 获取仓库、安装工具和权重。
2. 首次开展实验：读 [实验协议与输入边界](references/protocol.md)。精确参数在 [protocol.json](assets/protocol.json)，完整 ID 在 [scenes.json](assets/scenes.json)。
3. 协助者负责第 6–10 场景的在线下载与处理：读 [五场景官方下载与处理交接](references/dl3dv-download.md)。包含逐场景 Hugging Face 链接、相机适配方法、目录格式和验收要求；这项分工本身不启动建模。
4. 执行、巡检、回传：读 [运行与交接](references/run.md)。

## 不改变的实验条件

- 三组为 `single_basic`、`single_tools`、`harness_tools`；全组初始用户任务相同且简短。单 Agent 不要求提交初始化物体清单，也不安排独立计划 Agent。
- 单 Agent 只自行修订两次；不提供独立审核意见或 Harness 生成的对照面板、叠图、RGB 差异图。普通 Blender 预览可以使用。
- Harness 为计划 → SAM3 → 对象几何初始化 → M0 → R1 → M1 → R2 → M2；最后不追加 R3。物理暂不做。
- 当前批次采用 `intrinsic`：禁止原图直接投影、把阴影或其他物体烘进照片纹理；允许归属正确的干净物体纹理，材质与灯光显式配置。不混入早期 `captured` 或灰模消融。
- 48 个训练视角、6 个可见验证视角；私有测试为 12 个中心及其支持窗口。分割/DA3 只用训练输入。原始完整数据目录由控制器读取，不给建模 Agent 浏览。
- M0 最长 3600 秒；M1/M2 和审核无硬期限；计划与审核均无硬期限。新机器专业工具默认无硬期限，单列运行配置。不要因慢机器降低帧数、分辨率、采样数或换模型后仍称为相同实验。
- 保存 M0/M1/M2、各角色用量和失败。遇到明确错误保留原记录；不要删除失败重跑直到成功。基础设施恢复必须保留恢复原因、旧错误与新增用量，不算额外科学轮次。

## 最小执行顺序

1. 离线完整十场景包：校验 SHA256、解压并运行 `scripts/verify_dataset.py`。在线五场景分工：按 [下载与处理交接](references/dl3dv-download.md) 完成子集验收；无需下载其余五个场景。
2. 用 `scripts/configure.py` 创建本人的全新实验根目录与 `runtime.json`；它不会调用模型。
3. 运行 `scripts/preflight.py` 检查 Linux/systemd、Codex CLI、Blender、OIDN、Docker/CUDA、DA3/SAM3 和评测依赖；按安装文档做工具推理冒烟检查及模型访问探针。
4. 用仓库 `prepare.py` 先建立输入划分，再执行训练图 DA3/TSDF 初始化。检查生成记录，再启动 `monitor.py`。它同时调度建模和冻结评测。
5. 汇总 `results.csv` 和失败率，同时检查源图/新视角/逐物体编辑图。机械可操作成功不能称为语义完整分离。
6. 回传各自目录及清单；排除 `homes/`、认证文件和访问令牌。模型源码、冻结工程保留在运行端；统计与少量检查图可单独传递。

GitHub 仓库的旧 `experiments/reconstruction/protocol.json` 是历史实验，不是本次十场景协议；始终由本 Skill 的配置脚本生成根目录协议。本 Skill 不会接管其他已运行批次或自动启动全量 90 次试验。
