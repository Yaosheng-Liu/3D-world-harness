# 第6–10场景：交给协助者下载和处理

本次数据准备分工只包括下面五个场景，共1879帧。协助者负责下载原始ZIP、适配图像与相机、整理目录并回传校验记录；完成后交付，模型、方法与运行数量另按实验分工确定。无需下载第1–5场景，也不改场景选择或重新抽帧。

## 官方来源与固定场景

使用 [DL3DV-ALL-960P](https://huggingface.co/datasets/DL3DV/DL3DV-ALL-960P) 的图像与相机包；先由协助者本人登录Hugging Face并接受数据访问条件。[官方说明](https://github.com/DL3DV-10K/Dataset#dataset-download)支持按hash下载。

| 编号 | 场景 / key | 帧数 | 官方文件 |
| --- | --- | ---: | --- |
| 6 | 酒店双床房 / `hotel_twin` | 340 | [Hugging Face · 2K](https://huggingface.co/datasets/DL3DV/DL3DV-ALL-960P/blob/main/2K/3faa84ca98aa8d3794ea15d9b9ed61da35ae1755622af7857715a27ca61c7dea.zip) |
| 7 | 开放办公区 / `open_office` | 385 | [Hugging Face · 1K](https://huggingface.co/datasets/DL3DV/DL3DV-ALL-960P/blob/main/1K/3fae5e0bb1354fe30a0a23934ab00c7a51004027cc79ae1205f910b7bdefd33f.zip) |
| 8 | 小型健身房 / `compact_gym` | 408 | [Hugging Face · 1K](https://huggingface.co/datasets/DL3DV/DL3DV-ALL-960P/blob/main/1K/91d19c97d51ee43fbd3372025dfa1bb9230fc41f344585b1ff9d191e138c3510.zip) |
| 9 | 客厅厨房样板间 / `kitchen_living` | 330 | [Hugging Face · 1K](https://huggingface.co/datasets/DL3DV/DL3DV-ALL-960P/blob/main/1K/1e1a3e3bbab066fc0374dc6c40f4778d2c85221330fea126fdcb906980b2bb11.zip) |
| 10 | 带吧台的咖啡店 / `cafe_counter` | 416 | [Hugging Face · 2K](https://huggingface.co/datasets/DL3DV/DL3DV-ALL-960P/blob/main/2K/76a1bad134ddd1ebf98fc62fc1040f0febed3f4d08f54fd7aee502405a07ff53.zip) |

完整scene_id、相对下载路径`hf_path`、帧数与当前实验来源哈希均在[assets/scenes.json](../assets/scenes.json)。2026-11-10已通过官方文件列表核对这五个路径存在；本次没有重新下载五份大文件，受限文件的公开接口隐藏内容哈希，因此尚未确认它们与当前本机归档逐字节一致。

本机历史目录名`DL3DV-ALL-960P-undistorted`不是已核实的官方仓库名；不要据此拼下载地址或判断是否还需要去畸变。

## 下载方法

在协助者的Linux或WSL终端执行，路径自行选择。仓库已按[setup.md](setup.md)获取，`.venv`已安装`huggingface_hub`。

```bash
export WH_REPO=/your/repo/3D-world-harness
export WH_SKILL="$WH_REPO/skills/worldharness-experiments"
export WH_DOWNLOAD=/your/data/dl3dv-official-zips
export WH_DATA=/your/data/worldharness-scenes-6-10
"$WH_REPO/.venv/bin/hf" auth login
"$WH_REPO/.venv/bin/python" - <<'PYDOWNLOAD'
import json, os
from pathlib import Path
from huggingface_hub import hf_hub_download
selection = json.loads((Path(os.environ['WH_SKILL']) / 'assets/scenes.json').read_text())
for scene in selection['scenes']:
    if 6 <= scene['number'] <= 10:
        hf_hub_download(repo_id=selection['official_download_repository'],
                       repo_type='dataset', filename=scene['hf_path'],
                       local_dir=os.environ['WH_DOWNLOAD'])
PYDOWNLOAD
```

登录使用协助者自己的账号；交付包不放token。下载只包含上述五个ZIP，不包含模型权重。记录实际HF revision、各文件SHA256与字节数；用Python `zipfile.ZipFile.testzip()`检查ZIP完整性。

## 协助者需要完成的处理

在独立的`WH_DATA`目录完成以下操作，不修改原始ZIP：

1. 读取每个ZIP中`<scene_id>/images_4/frame_*.png`和`<scene_id>/transforms.json`。保留完整图像，不重编码、不裁剪、不缩放、不重新从视频抽帧。图像与相机引用必须按文件名一一对应，无缺失或重复。
2. 原相机文件原样保存为`transforms_original.json`。读取PNG的实际宽高`W,H`，把相机像素内参从原`w,h`缩放到实际图像尺寸：`sx=W/w`、`sy=H/h`，`fl_x,cx`乘`sx`，`fl_y,cy`乘`sy`；顶层和逐帧内参都处理。不要固定除以4，已经匹配尺寸时比例就是1。
3. 把帧引用统一为`images_4/<原文件名>`，更新已有尺寸字段；保留位姿矩阵、相机坐标约定、尺度、畸变系数与其他字段。这里仅适配像素分辨率，不重新估计相机、不额外去畸变。
4. 将处理后的相机存为`transforms.json`，使用`json.dumps(..., ensure_ascii=False, indent=2) + '\n'`序列化。保存`camera_info.json`：原始尺寸、实际尺寸、缩放比例、处理脚本版本。

下面是第2–4步的相机处理核心，可放入协助者自己的整理脚本；它与本批已有输入的缩放规则一致：

```python
import copy
import json
from pathlib import Path


def adapt_camera(original, width, height):
    camera = copy.deepcopy(original)
    sx, sy = width / original['w'], height / original['h']
    camera['w'], camera['h'] = width, height
    for block in [camera, *camera['frames']]:
        for key in ('fl_x', 'cx', 'fl_y', 'cy'):
            if key in block:
                block[key] *= sx if key in ('fl_x', 'cx') else sy
    for frame in camera['frames']:
        frame['file_path'] = 'images_4/' + Path(frame['file_path']).name
        if 'w' in frame:
            frame['w'] = width
        if 'h' in frame:
            frame['h'] = height
    return camera

# original = json.loads(original_bytes)
# width, height = Image.open(first_png).size  # Pillow；其余PNG也检查同尺寸
# adapted = adapt_camera(original, width, height)
# output.write_text(json.dumps(adapted, ensure_ascii=False, indent=2) + '\n')
```

5. 保存来源记录`source.json`：scene_id、仓库、HF revision、hf_path、实际ZIP SHA256、字节数、下载时间。将ZIP SHA与`assets/scenes.json`的`source_archive_sha256`比较，处理后相机SHA与`camera_sha256`比较。如果不一致，回传差异和原相机字段，先查清数据版本/处理差异，不修改共享基准哈希或把不同输入并入同一批实验。
6. 建立根目录`manifest.json`和`checksums.json`。manifest只列实际完成的这五个场景；每条从`assets/scenes.json`取对应记录，增加`path: "scenes/<scene_id>"`，不要声称本地已有十场景。checksums是所有交付图像和相机/来源文件的`相对路径: SHA256`映射，不含它自身。

最终目录：

```text
worldharness-scenes-6-10/
  manifest.json              # dataset: DL3DV, selection_version: 3,
                             # total_frames: 1879, scenes: [五条记录]
  checksums.json
  preparation_report.json
  scenes/<scene_id>/
    images_4/frame_*.png
    transforms_original.json
    transforms.json
    camera_info.json
    source.json
```

观看副本可选生成10fps的`preview.mp4`，标为“原始帧序列观看副本”；它不是原视频帧率，也不是重建结果，不作为实验输入。

## 验收与回传

协助者在`preparation_report.json`逐场景记录以下实际结果：

- key、完整ID、HF路径/revision，实际和基准ZIP SHA、相机SHA及是否相同。
- 图像数、相机数分别为340/385/408/330/416，合计1879；文件名集合一致、无重复引用；所有图片可解码且尺寸与适配相机匹配。
- 位姿是有限的4×4矩阵，处理前后逐帧位姿和畸变系数未变；记录内参缩放比例。
- `checksums.json`所有文件逐一复核通过；保存实际使用的处理脚本，不只回传“完成”。

当前`scripts/verify_dataset.py`只验完整十场景离线包，因此五场景按本节验收，不运行该脚本后为了过关伪造另外五条记录。`scripts/configure.py`支持子集manifest；后续明确开始模型实验时，按[run.md](run.md)配置`--dataset "$WH_DATA"`并指定：

```text
--scenes hotel_twin,open_office,compact_gym,kitchen_living,cafe_counter
```

其余模型、推理等级、工具与输入划分沿用固定协议。由`prepare.py --stage inputs`生成48训练/6验证/12私有测试中心与支持窗口，不在整理阶段手工选训练或测试帧。需要执行初始化时，再运行`--stage initialize`；本次下载处理分工不自动启动这些模型/工具阶段。

交付处理脚本、manifest、checksums和preparation_report，以及数据目录的位置或压缩包；原始ZIP可保留在下载端，无需重复打包。不要附账号、认证目录、旧建模结果或代理配置。
