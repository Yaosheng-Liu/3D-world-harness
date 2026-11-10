# 分工执行、巡检和回传

以下变量都是本机选择的绝对路径。工具依赖先按[setup.md](setup.md)装好。数据包顶层目录是`worldharness-dl3dv10`。

## 1. 数据校验

以下命令针对完整十场景离线包。只负责第6–10场景的协助者，先按[官方下载与处理交接](dl3dv-download.md)生成五场景数据根目录并完成子集验收，再从第2节继续；`verify_dataset.py`固定要求全部十场景，不能用于五场景子集。

```bash
sha256sum -c worldharness-dl3dv10-v3.zip.sha256
unzip worldharness-dl3dv10-v3.zip -d /your/data
python3 /your/repo/skills/worldharness-experiments/scripts/verify_dataset.py /your/data/worldharness-dl3dv10
```

Windows可用`Get-FileHash -Algorithm SHA256`检查压缩包；实际运行和解压建议在WSL内。十场景key：`bedroom,study,gallery,company_lounge,home_living,hotel_twin,open_office,compact_gym,kitchen_living,cafe_counter`。数据是完整原始帧和匹配相机，不能把10fps观看副本当原始采集视频或重建视频。

## 2. 生成个人实验配置

示例为Luna负责健身房和咖啡店，共6次运行。按分工修改`--model`和`--scenes`，不要默认替别人再跑全部任务：

```bash
export WH_REPO=/your/repo/3D-world-harness
export WH_DATA=/your/data/worldharness-dl3dv10
export WH_TOOLS="$HOME/worldharness-tools"
export WH_EXP=/your/results/participant-a-luna
export WH_SKILL="$WH_REPO/skills/worldharness-experiments"
export PATH="$WH_REPO/.venv/bin:$PATH"
python "$WH_SKILL/scripts/configure.py" \
  --repo "$WH_REPO" --dataset "$WH_DATA" --root "$WH_EXP" --tools "$WH_TOOLS" \
  --model gpt-6-luna --scenes compact_gym,cafe_counter --workers 1 \
  --codex "$WH_TOOLS/codex-experiment/node_modules/.bin/codex" --auth-home "$HOME/.codex" \
  --blender /usr/bin/blender --oidn "$WH_TOOLS/oidn/build/libOpenImageDenoise.so.2.5.1"
python "$WH_SKILL/scripts/preflight.py" --runtime "$WH_EXP/runtime.json" --output "$WH_EXP/preflight.json"
```

按实际位置修改OIDN动态库路径。运行根目录必须全新，脚本不会覆盖旧试验。`runtime.json`只保存本机路径/镜像/代理，不含密钥，但无需公开。协议中的`suite_protocol_sha256`标识共享模板；具体`protocol_sha256`因个人场景、模型、根目录ID而不同，合并时同时核对模板SHA和实际参数。每个root仅一种模型，计划/审核自动与它一致；配置脚本为Astra/Sol选ultra、Luna选xhigh。

完成工具实际推理冒烟及只读模型READY探针；再执行：

```bash
python "$WH_REPO/experiments/reconstruction/prepare.py" --root "$WH_EXP" --runtime "$WH_EXP/runtime.json" --stage inputs
python "$WH_REPO/experiments/reconstruction/prepare.py" --root "$WH_EXP" --runtime "$WH_EXP/runtime.json" --stage initialize
```

检查`shared/*/prepared.json`的划分、`calibration_audit.json`、`initialization.json`及`evaluation_private/*/reference_report.json`。未成功预处理的场景先标阻塞，不用半份缓存开始试验。不同设备的相机参数由原输入保持，不能为某一种方法单独拟合相机。

## 3. 启动与监控

```bash
nohup python -u "$WH_REPO/experiments/reconstruction/monitor.py" \
  --root "$WH_EXP" --runtime "$WH_EXP/runtime.json" --interval 60 \
  > "$WH_EXP/monitor-launch.log" 2>&1 &
```

一个root只启动一个monitor。`scheduler.json`可用`allowed_conditions`先只放行`single_basic`，待工具初始化完成后开放其他方法；`condition_limits`可按方法限定在运行的任务数，实现单Agent与工具组混排；`min_free_disk_bytes`只暂停新任务启动，不终止已有任务。同设备各root共用`runtime.json`中的`gpu_lock`，SAM3与DA3推理按锁串行，Agent对话可并发。它根据`scheduler.json`启动建模worker、评测worker并更新`monitor.json`；不要同时再无过滤启动`run.py`。先1个worker，确认GPU/RAM足够后显式改`workers_per_model`；多GPU可拆成独立root并通过`--docker-gpus device=N`分配。该参数管专业工具CUDA，不等于Blender渲染GPU选择；当前固定评测代码使用CPU Cycles/OIDN。

```bash
python "$WH_REPO/experiments/reconstruction/report.py" --root "$WH_EXP"
cat "$WH_EXP/monitor.json"
cat "$WH_EXP/STATUS.md"
```

完成判据：所有分配试验到`modeled`并有M0/M1/M2冻结文件，所有检查点评测完成，`automated_finished=true`且无未处理错误。即使自动阶段完成，语义编辑核验仍需看图。monitor会把失败保留下来，连续两次失败创建`launch_hold.json`；这会阻止新任务，不强杀正在进行的任务。

## 4. 明确失败时如何处理

- 旧计划900秒超时：运行配置`planner_timeout_seconds`设为`null`后，monitor保存原状态和用量并从原planner线程续接；原事件日志由计划器归档，累计Token不重复相加，不将超时当作规划质量失败。
- 网络或模型访问失败：记录具体调用与已有Token，不改模型名。
- `Textured geometry requires UV coordinates`：模型提交的带贴图网格缺UV，是实际构建错误；不删掉校验或由协调者悄悄改模型。保留失败，若额外安排工程恢复，单列恢复轮次/用量及口径。
- SAM3长时间无输出：先检查对应容器、GPU利用率、内存和任务输入。旧版本1小时超时可能留下容器；新交接配置取消该工具硬期限，但不能把“没有超时”当完成。确认退出的控制器所属容器后才清理，不停其他人的任务。
- OOM：降低并发或换设备；不降低本批科学参数。
- 有源文件但没有冻结版本：只能报告“已生成候选，未成功构建”，不能计作M0完成。

不要删除`launch_hold.json`后无差别重试。协调者决定恢复时保存旧status、原因、输入/候选SHA和用量；保留原失败结果。用户仅让查看状态时，不擅自修改候选或恢复旧实验。

## 5. 回传与汇总

最小回传：`protocol.json`、`scene_selection.json`、`preflight.json`、`inputs.json`、`initialize.json`、`shared/*/prepared.json`及`initialization.json`、`monitor.json`、`progress.json`、`results.csv/json`、每次`spec.json/status.json`与`usage.json`、各检查点`frozen.json`、每个测试视角的图像/空间指标、代表性渲染和编辑图。旧批次的`initialization_summary.json`若存在也保留。冻结工程、candidate、脚本和纹理可作为独立成果包回传，保留哈希。

不要打包整个experiment-root：`runs/*/homes/`含认证和会话。现有`sync_records.py --help`提供允许清单同步，可用于SSH复制小记录；工程成果另按明确文件清单打包。公开原始events/提示词前检查是否含本地环境或敏感内容。

协调者按`scene_id + model + effort + condition + checkpoint + suite_protocol_sha256`核对重复，先列失败/缺失，再汇总成功指标。保留预处理成本与Agent成本两张表；不同硬件墙钟时间分开报告。已有单Agent结果按相同输入、外观规则、提示词、轮次和等级核对后可登记复用，保留原始协议SHA；混合角色Harness不得作为同模型Harness结果并入。
