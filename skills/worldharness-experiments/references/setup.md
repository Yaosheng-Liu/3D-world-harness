# 安装、下载与设备适配

本批运行端是 Linux + NVIDIA CUDA；工作台浏览器可以在任意电脑。以下命令在运行端执行，路径自行选择。实际测试环境为 Linux ARM64 / Spark、Blender 4.0.2、OIDN 2.5.1；x86/WSL 配方需在目标设备通过预检和推理冒烟，不能声称已在所有设备验证。

| 设备 | 运行方式 |
|---|---|
| Linux x86_64 + NVIDIA GPU | 原生控制器、Blender，Docker 内 DA3/SAM3 |
| Linux ARM64 + NVIDIA GPU | 同一代码；容器和依赖必须为 ARM64，Open3D 可能需要源码编译 |
| Windows + NVIDIA GPU | WSL2 Ubuntu 中运行；启用 WSL systemd，安装 WSL 可用的 NVIDIA 驱动及 Docker GPU 支持；数据放 Linux 文件系统 |
| Mac、无 NVIDIA GPU 的电脑 | 浏览器/VS Code SSH 连接上述 GPU 主机；当前工具链不提供 MPS/CPU 的完整实验路径 |

DA3 当前一次处理48张756分辨率训练图，不在此承诺某个显存容量一定够。先单任务验证；OOM 后记录并换更大显存主机，不能静默改帧数或分辨率。并发从1开始，按GPU和RAM实测提高；`--docker-gpus device=0`可绑定设备。工具和容器缓存可能占几十GB，冻结工程随试验增长，先检查磁盘。

## GitHub 与主机环境

```bash
git clone --branch feat/portable-experiment-handoff https://github.com/Yaosheng-Liu/3D-world-harness.git
cd 3D-world-harness
git rev-parse HEAD
```

**Skill交接包根目录**的 `release.json` 记录精确 commit（不在2.69GB数据包内）；共同运行时 checkout 该 commit，不跟随移动分支。将整个 `worldharness-experiments` Skill 文件夹复制到个人 `~/.codex/skills/` 即可安装，也可让 Agent 直接读取仓库中 `skills/worldharness-experiments/SKILL.md`。

Ubuntu 主机安装 Git、Python3.12/venv、FFmpeg、Blender4.0.2、Node/npm、Docker Engine、NVIDIA Container Toolkit。系统依赖用本机包管理器安装；例如 Ubuntu24.04：

```bash
sudo apt-get update
sudo apt-get install git python3-venv python3-dev ffmpeg blender nodejs npm cmake build-essential
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m pip install numpy==1.26.4 opencv-python-headless==4.9.0.80
blender --version
nvidia-smi
systemd-run --user --scope --quiet /bin/true
```

Blender 默认程序名不是固定路径；配置时传真实可执行文件。正式对比使用4.0.2；其他版本应单列环境差异。Docker/NVIDIA 安装按[官方工具包说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)，完成后先验证容器能看到CUDA。不自动修改主机 AppArmor 或全局安全配置；若 Codex 沙盒启动失败，报告具体原因，在该主机按管理员批准的方式启用所需功能。

## Codex：实验和工具台分开安装

[官方 CLI 安装](https://learn.chatgpt.com/docs/codex/cli)、[认证说明](https://learn.chatgpt.com/docs/auth)。每位参与者使用自己的账号；不要从交付包寻找或复制发起人的认证。

```bash
export WH_TOOLS="$HOME/worldharness-tools"
mkdir -p "$WH_TOOLS"
npm install --prefix "$WH_TOOLS/codex-experiment" @openai/codex@0.164.0
npm install --prefix "$WH_TOOLS/codex-workbench" @openai/codex@0.156.1
"$WH_TOOLS/codex-experiment/node_modules/.bin/codex" --version
"$WH_TOOLS/codex-experiment/node_modules/.bin/codex" login
```

当前批量脚本使用CLI 0.164.0的`exec --ignore-user-config`；网页App Server适配器固定0.156.1。不要用同一个全局升级覆盖两者。实验每个角色创建独立会话目录，从本人的`auth_home/auth.json`复制认证，文件权限0600。该目录不可回传。

指定模型名是本项目实验配置，不保证任意账号可访问。用本人账号在临时空工程中以目标模型/xhigh执行一句“仅回复READY”的只读探针，保存版本、成功/失败和探针用量，排除正式试验成本。若不可访问，不自行换成别的模型。网络代理只在需要时通过`--proxy`配置自己的地址，默认空字符串；不要照搬原主机127.0.0.1端口。

```bash
mkdir -p "$WH_TOOLS/installation-smoke/model-probe"
"$WH_TOOLS/codex-experiment/node_modules/.bin/codex" exec \
  --ignore-user-config --skip-git-repo-check --json \
  -C "$WH_TOOLS/installation-smoke/model-probe" -s read-only \
  -c 'approval_policy="never"' -m gpt-6-luna -c 'model_reasoning_effort="xhigh"' \
  '只回复READY，不调用工具。' > "$WH_TOOLS/installation-smoke/model-probe/events.jsonl"
```

按实际分工替换模型；该探针会调用模型，因此只在已获该模型试验授权的账号执行。

## 下载 DA3 和 SAM3

保持下列目录约定，所有绝对路径由配置脚本从`WH_TOOLS`生成。

```bash
git clone https://github.com/ByteDance-Seed/Depth-Anything-3.git "$WH_TOOLS/Depth-Anything-3"
git -C "$WH_TOOLS/Depth-Anything-3" checkout 3d835ec1a5802d64a8b8b15f817a1ab54809bfe4
git -C "$WH_TOOLS/Depth-Anything-3" apply "$PWD/skills/worldharness-experiments/assets/da3-optional-export.patch"
git clone https://github.com/facebookresearch/sam3.git "$WH_TOOLS/sam3"
git -C "$WH_TOOLS/sam3" checkout 5dd401d1c5c1d5c3eedff06d41b77af824517619
.venv/bin/python -m pip install 'huggingface_hub[cli]==0.36.2'
.venv/bin/hf auth login
.venv/bin/hf download depth-anything/DA3NESTED-GIANT-LARGE --local-dir "$WH_TOOLS/Depth-Anything-3/checkpoints/DA3NESTED-GIANT-LARGE"
.venv/bin/hf download facebook/sam3 sam3.pt --local-dir "$WH_TOOLS/weights/sam3"
```

SAM3权重需要本人获得[官方模型访问权限](https://github.com/facebookresearch/sam3#installation)。下载到本地后，推理容器关闭网络。DA3使用[官方DA3NESTED-GIANT-LARGE](https://github.com/ByteDance-Seed/Depth-Anything-3#-quick-start)，不使用SAM3D Objects、VGGT或ViTPose代替；它们不属于本批静态场景对照的必要工具。

附带DA3补丁来自当前实验，只使无关的COLMAP/高斯视频导出依赖可选，不改变本实验深度推理算法。保存补丁和源码版本；不要自行升级SAM3.1或新DA3。

## 通用工具容器

仓库提供`assets/Dockerfile.tools`，复用支持目标架构的CUDA/PyTorch基镜像，安装DA3/SAM3所需Python依赖及Open3D/LPIPS。默认参考基镜像为`nvcr.io/nvidia/pytorch:26.04-py3`（与原运行环境相同系列）。本地拉取权限、驱动与架构需先检查；可显式指定已批准的兼容基镜像，并记录差异。

```bash
docker build -f skills/worldharness-experiments/assets/Dockerfile.tools \
  -t worldharness-tools:20261110 skills/worldharness-experiments/assets
```

构建失败时修复明确依赖后重建，不换工具算法。x86安装Open3D0.19.0 wheel；ARM64源码构建CPU Open3D，耗时较长。Spark已有两个原镜像可以在`configure.py`显式传入`--geometry-image`和`--segmentation-image`；这些本地镜像名不是公开下载地址。

预下载评测AlexNet权重（LPIPS自身版本0.1.4固定在镜像内）：

```bash
mkdir -p "$WH_TOOLS/torch-cache" "$WH_TOOLS/evaluation-packages"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$WH_TOOLS/torch-cache:/cache" -e TORCH_HOME=/cache \
  --entrypoint python worldharness-tools:20261110 \
  -c 'from torchvision.models import alexnet,AlexNet_Weights; alexnet(weights=AlexNet_Weights.IMAGENET1K_V1)'
```

`evaluation-packages`是旧环境的兼容挂载点；新镜像已有LPIPS，可留空。正式评价离线读取权重，不临时联网下载。

## OIDN2.5.1 与工作台

到[OIDN2.5.1发行页](https://github.com/RenderKit/oidn/releases/tag/v2.5.1)下载对应Linux架构发行包；把真实`libOpenImageDenoise.so.2.5.1`传给配置。无对应预编译包时，从该tag按[官方构建步骤](https://github.com/RenderKit/oidn#compiling-from-source)构建CPU版：

```bash
git clone --branch v2.5.1 --recursive https://github.com/RenderKit/oidn.git "$WH_TOOLS/oidn"
cmake -S "$WH_TOOLS/oidn" -B "$WH_TOOLS/oidn/build" -DCMAKE_BUILD_TYPE=Release \
  -DOIDN_DEVICE_CPU=ON -DOIDN_DEVICE_CUDA=OFF -DOIDN_DEVICE_HIP=OFF -DOIDN_DEVICE_SYCL=OFF
cmake --build "$WH_TOOLS/oidn/build" --parallel 4
```

源码构建还需官方要求的ISPC/TBB，按对应架构安装；不要拿Blender内部另一个版本的降噪器替换。运行评测前预检动态库可加载。

工具台从仓库启动：

```bash
export PATH="$WH_TOOLS/codex-workbench/node_modules/.bin:$PATH"
.venv/bin/python scripts/start_blank_workbench.py --port 18776
```

在浏览器打开`http://127.0.0.1:18776/`，通过“打开文件夹”查看工程。正式批量试验由CLI运行；查看冻结版本时将其复制到独立展示目录，不向正式Agent发送人工修改意见，也不编辑冻结成果。页面会话不等于批量实验会话。

远程访问：VS Code Ports转发远端18776，浏览器使用面板显示的**本地端口**；也可在自己电脑运行`ssh -N -L 18776:127.0.0.1:18776 <主机别名>`。白页时分别检查远端和本地的`/api/health`，并让localhost绕过代理，避免重复改渲染代码排查传输故障。

## 推理冒烟检查

在正式初始化前，用单独目录和两张训练图测试DA3加载、GPU推理和输出有限深度；用一张训练图和一个简单名词测试SAM3。示例入口见`sam3`官方image predictor和DA3官方API，保持上述模型权重。测试记录写到`installation-smoke/`，不混入正式Agent轮次或私有测试图。`preflight.py`只证明依赖导入和设备可用，不能代替实际推理或证明48帧显存足够。
