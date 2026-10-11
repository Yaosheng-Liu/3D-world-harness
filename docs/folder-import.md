# 从服务主机文件夹导入已准备场景

## 空白工作台入口

启动独立空白入口：

```bash
.venv/bin/python scripts/start_blank_workbench.py
```

默认访问 `http://127.0.0.1:18776/`。首页显示「打开文件夹」，导入后从左侧列表选择场景。直接打开根地址始终进入空白首页，已有导入实例保留在列表，卸载当前场景后返回空白首页。

默认工作目录与数据目录分别为 `~/.local/share/scene-feedback/blank-workbench/workspace` 和 `~/.local/share/scene-feedback/blank-workbench/data`。可用 `--project-dir`、`--data-dir` 指定独立位置；它们不复用旧重建服务的数据。误指定含模型、参考图、任务或反馈的根数据时，启动器报错并保留原数据，需选择新目录。

局域网启动示例（将 IP 换为服务主机地址）：

```bash
.venv/bin/python scripts/start_blank_workbench.py \
  --port 18776 \
  --listen-host 0.0.0.0 \
  --public-base-url http://192.168.3.157:18776 \
  --lan-access open
```

此模式可直接收藏根地址 `/`，不需要会话参数或访问凭证链接。通过工作台服务主机文件夹导入场景；启动采用 MCP Events 模式，不自动创建或绑定 Codex 任务。新导入场景默认使用插件订阅投递，也可在选中场景后明确连接或新建 Codex 对话，见下文。

## 打开与导入

在侧栏「场景」旁点击文件夹图标「打开文件夹」。这里浏览的是**工作台服务所在机器**的目录；通过 LAN 打开的浏览器不会读取客户端电脑的本地文件夹。可以逐级浏览，也可以粘贴服务主机上的绝对路径。

点击「打开并导入」后，服务在扫描边界内递归识别已准备实例，并把成功导入的场景加入列表。当前正在编辑的场景、草稿和接收任务保持原样；导入不会自动创建 Codex 任务。之后可从列表选择导入的场景，需要模型继续处理时再明确连接或创建任务。

每个导入场景独立保存工作台数据与反馈。导入过程只读取来源素材，不改原 GLB、可编辑 `.blend`、参考图或参考清单。

### 为导入场景连接或新建对话

先在左侧列表选择一个导入场景，再打开「任务」，选择服务主机上的现有 Codex Desktop 任务，或填写名称、模型、思考强度与权限新建当前场景的会话。服务发现同一用户本机上唯一可核验的 Desktop 连接，不需要先把空白首页绑定到某个任务；连接不可用或不唯一时显示具体原因。浏览器可运行在其他 LAN 设备，连接的是服务主机的桌面对话。协议背景见 [Codex App Server](https://learn.chatgpt.com/docs/app-server)。

明确连接成功后，该场景后续反馈直接送入选定对话，保存绑定，并保留原 Events 来源及历史；新反馈不会同时走事件与对话两条投递路线。旧事件与 outbox 不因连接操作重新回放或转送到新对话。工作台的模型、参考图、相机、标记与草稿保留，其他场景的投递方式不变。重启、卸载后重新导入均恢复该场景已保存的绑定与结果。

空白首页只提供场景入口，不能绑定或新建自己的对话；须先选中导入场景。Events 服务的「新建场景」页仍未启用，这里的「新建对话」用于当前已导入场景。

### 引用其他场景作为参考

在左侧其他场景行点击引用按钮，选取**同一工作台中已加载且可用的其他场景**。引用显示为 `🏞️1` 等短编号，可在一段提示中说明要借鉴哪部分。场景参考从侧栏选择，`@` 候选范围保持原样；当前场景、视角和任务绑定保持原样，也不会连接或启动来源场景的任务。此入口只在当前工作台列表内选择，不跨服务引用。

引用时只读捕获来源场景的版本与几何，把场景 JSON、完整 GLB 和可用的首张 GT 原图复制到当前场景的引用档案，再在后台尝试生成模型预览；服务确认快照未变后，已生成的预览可跨新一轮和页面刷新复用。预览不可用但有 GT 时使用 GT，二者都不可用则明确报错，不加入引用。来源场景本身保持只读，其会话文字、任务和凭证不会复制。已捕获的副本在来源更新或卸载后继续可用。模型接收场景引用的名称、版本与归档路径摘要，以及预览和可用 GT 图；完整几何按归档路径读取，不塞进提示正文。

每轮反馈最多引用 4 个场景，GLB 复制预算为每个场景 200 MiB、每轮反馈合计 400 MiB；单个 GLB 仍须通过既有大小、容器与内嵌资源校验。来源不可用、素材损坏或超过预算时会报错。删除引用仅移除本轮引用，已提交反馈中的固定证据保留。

## 导入前检查 ready

点击「打开并导入」时，服务会先自动检查待导入实例的模型、参考图、相机和帧时间。检查通过的实例加入场景列表，有使用限制的实例继续导入并显示提示；无法通过的实例报出具体原因，其余有效实例继续导入。目录里若没有可导入实例，会显示失败或未发现实例的原因。检查使用当前工作台的素材读取与导入校验；视频会由现有 FFmpeg 在临时目录内取样检查，临时文件随后清理。

新实例检查所选来源包。已注册实例再次导入时，检查并复用已经保存的工作台模型与参考数据；卸载后恢复也保留已有修改，不拿来源初始模型覆盖保存结果。独立 CLI／skill 自查始终检查传入的来源文件夹，不能据此判断另一份已保存工作台结果是否有效。排查重新导入失败时，应区分报告检查的是来源包还是已保存结果。

导入结果包含各实例的检查情况。独立 CLI 和下面的 skill 也能提前生成只读报告，检查报告使用这些状态：

| 结果 | 含义 |
| --- | --- |
| 可导入 `ready` | 该实例在本次检查中通过，已声明的资料完整 |
| 可导入，有提示 `warning` | 素材可读且可以导入，但存在报告中的使用限制 |
| 部分可导入 `partial` | 一些实例可导入，但另有失败实例或扫描已截断 |
| 无法导入 `blocked` | 没有通过的实例，报告给出阻止原因 |
| 未发现实例 `empty` | 在当前扫描边界内没有识别到可检查的 ready 实例 |

父目录清单中的仅模型实例仍可导入，缺参考图与相机会显示提示。部分参考帧未附相机、静态图没有导入用相机或未提供 Blender 源工程也属于提示：仍可手动比较。已声明却损坏的模型、参考像素、相机或时间字段会阻止该实例导入并报错；单项失败不会撤销或阻止其他已通过实例。显式 `ready:false` 的实例列为跳过，不按已准备模型检查。

检查范围包括 GLB 2.0 容器、BIN 内嵌资源与导入结构、当前视口的压缩解码器兼容性、参考 PNG/JPEG 的完整解码及尺寸、机位清单、帧率、精确时间戳、相机格式与图片尺寸匹配。沿用已有限制：GLB 不超过 100 MiB，参考图不超过 25 MiB，静态图至多 8 张，参考机位至多 8 个，每机位至多 600 帧；图片宽高均不超过 32,768 像素且总像素不超过 5,000 万。路径边界、清单大小、视频大小和目录扫描边界仍按当前导入规则检查。

报告中的动画数量与时间跨度来自 GLB 元数据；它不运行 GPU 推理，不重开 Blender，也不证明动画播放、几何重建、人体追踪或视觉对齐质量。Blender 源文件只检查是否已提供且可读。参考图片像素检查证明文件可解码，不证明模型与照片的位置、比例或遮挡正确。独立自查只反映检查当时的来源内容；新实例导入时会重新校验来源，已注册实例则校验保存的工作台结果。

### Agent 交付前自查

插件包含 [`workbench-ready-check` skill](../skills/workbench-ready-check/SKILL.md)。可在让 agent 交付场景前要求它自查：

```text
请用 $workbench-ready-check 检查 /absolute/path/to/ready-scenes，处理阻止导入的问题，并说明仍有的使用限制。
```

skill 使用 `scripts/check_workbench_ready.py --json` 读取同一份检查报告；独立检查只读取来源，不创建工作台、导入场景或启动模型任务。插件安装目录使用随包附带的检查脚本，本机也可使用仓库脚本。它要求按实际报告说明可导入、使用提示、阻止项和扫描截断，不能把至少一个实例可导入解释成整个目录完全通过。

### 命令行与 JSON 报告

在仓库根目录运行：

```bash
.venv/bin/python scripts/check_workbench_ready.py \
  /home/nortonii/assembly101/workbench_ready_scenes
```

默认输出中文实例摘要；加 `--json` 输出包含 `status`、`can_import`、`counters`、各实例 `checks` 和 `metrics` 的完整 JSON：

```bash
.venv/bin/python scripts/check_workbench_ready.py \
  /home/nortonii/assembly101/workbench_ready_scenes \
  --json --output /tmp/workbench-ready-report.json
```

命令默认不写报告或工作台数据。只有明确指定 `--output` 时才创建该 JSON 文件，已有文件不会覆盖。退出码 `0` 表示检查范围内所有实例可导入（允许提示）；`1` 表示部分失败、全部失败、未发现实例或扫描截断；`2` 表示参数、目录或报告文件错误。`can_import:true` 只表示至少一个实例可导入，不能代替整体 `status`。扫描截断时请选择更具体的子目录再检查。

## 标准实例标识

标准实例在根目录放一份 `workbench-ready.json` 或 `workbench_ready.json`；这两个文件名都支持下表的 `schema_version: 1` 格式。保留其中一份即可，同时存在会报告标识歧义。已有视频 ready 包另按下文的兼容字段读取，无须补 `schema_version`。

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 必须为整数 `1` |
| `name` | 可选场景名；省略时使用实例目录名 |
| `scene_glb_path` 或 `glb` | 要导入的 GLB 路径 |
| `reference_manifest` | 可选动态参考清单路径 |
| `reference_clip: {"manifest_path": "…"}` | 动态参考清单的另一种写法 |
| `reference_images` | 可选静态参考图路径列表 |
| `ready` | 明确为 `false` 时跳过该实例 |

必须有参考清单或至少一张静态参考图，也可同时提供两者。GLB、参考图、视频与清单仍使用工作台既有的文件和素材校验；标识存在不代表损坏的素材可以导入。

### 静态参考图实例

实例根目录中的 `workbench-ready.json`：

```json
{
  "schema_version": 1,
  "name": "静态物品",
  "scene_glb_path": "output/scene.glb",
  "reference_images": [
    "references/front.png",
    "references/side.png"
  ]
}
```

### 动态／多机位实例

实例根目录中的 `workbench_ready.json`：

```json
{
  "schema_version": 1,
  "name": "动态片段",
  "glb": "output/scene.glb",
  "reference_clip": {
    "manifest_path": "references/multiview.json"
  }
}
```

也可用 `"reference_manifest": "references/multiview.json"` 指定同一份清单。动态清单保留机位、帧、精确 `time_sec` 与相机信息，格式沿用 [README 的动态参考说明](../README.md)。支持清单内直接声明 `views[].frames`，也支持 `views[].manifest_path` 指向各机位的帧清单。

例如，选中的父目录可以包含这两个独立实例：

```text
/absolute/path/ready-scenes/
├── static-item/
│   ├── workbench-ready.json
│   ├── output/scene.glb
│   └── references/
│       ├── front.png
│       └── side.png
└── motion-item/
    ├── workbench_ready.json
    ├── output/scene.glb
    └── references/
        ├── multiview.json
        └── camera-A/
            ├── frames.json
            └── frame_000.png
```

## 已有视频 ready 包兼容

已有实例根目录的 `workbench_ready.json` 也可使用 `status: "ready"`、`scene_glb` 与 `reference_sequence`，不要求重写成标准标识或添加 `schema_version`。例如：

```json
{
  "status": "ready",
  "name": "A · Escalator",
  "scene_glb": "dynamic_scene.glb",
  "editable_blend": "dynamic_scene.blend",
  "reference_sequence": "reference_clip.json",
  "camera_manifest": "camera_manifest.json",
  "world_up": "Z"
}
```

`scene_glb` 指定现有模型，`editable_blend` 指向可编辑源文件，`reference_sequence` 指向已有视频帧清单。相对路径以实例根目录为基准，仍须满足实例内路径边界。单机位的 `camera_manifest` 会导入对应相机，若序列也声明 `camera_manifest_path`，两者须指向同一文件；多机位在各自的参考清单中声明相机。帧时间与相机标定直接沿用，无须重新准备素材。`world_up` 可指定 `Y` 或 `Z`，作为自动地面方向，不旋转原几何。

例如一个父目录下的 `A`–`H` 子目录可分别放这种 ready 包。已有 `reference_clip.json` 的 120 帧、24 FPS、5 秒片段继续作为该实例的参考序列，原 GLB、`.blend` 与相机文件不被改写。

## 父目录的场景清单

同时支持父目录中的 `manifest.json` 以 `scenes` 列表声明实例，不要求每个子目录再提供 ready 标识：

```json
{
  "scenes": [
    {
      "name": "消防车",
      "folder": "fire_truck",
      "glb": "fire_truck/scene.glb",
      "blend": "fire_truck/scene.blend"
    }
  ]
}
```

`folder`、`glb`、可选 `blend` 和参考路径相对于清单所在目录解析；模型与参考素材必须位于声明的实例目录内。按清单指定的模型导入，不搜索或猜测另一份模型。`source_glb`、`source_blend`、`workbench_url` 等来源记录不用于读取外部素材或打开其他工作台。

此格式允许只包含模型的实例：缺少参考图时仍可加入场景列表，界面提示「只包含模型，尚无参考图」。已有 `reference_manifest`、`reference_images` 等可选参考字段会随模型导入。实例名称沿用清单，重复来源复用现有实例，单个坏条目不影响其他模型。

## 路径与兼容布局

标识中的素材路径可使用绝对路径，或相对于**实例根目录**的路径。路径解析后的真实文件必须仍在该实例根目录内；通过 `..` 或符号链接逃出实例的路径会拒绝。

参考清单内的路径则相对于**声明它的清单所在目录**解析。例如，`references/multiview.json` 的 `views[].manifest_path` 写 `camera-A/frames.json`，而该机位清单中的帧路径写 `frame_000.png`。帧路径、视频路径、相机清单及嵌套机位清单都须满足同一实例根目录边界。

已有目录也支持以下识别方式，优先级在实例根目录 ready 标识之后：

- `workbench/active_scene.json`：读取现有 `glb`、`reference_manifest` 与可选 `blend` 字段；相对路径仍以实例根目录为基准。`blend` 保留可编辑来源信息，不在导入时修改。
- `references/multiview.json` 加 `output/` 中唯一的 `.glb`：清单必须是多机位组，输出目录有且只有一份 GLB 才能选定模型。多个 GLB 时须添加显式实例标识指定模型；不会按修改时间、文件名或旧脚本猜测。

识别到一个实例后，不再把它内部的素材与历史树继续当作独立实例扫描。缺文件、坏 JSON、无参考资料或模型歧义会记录该实例的原因，继续处理其他实例。

## 扫描边界与结果

默认以所选目录为深度 `0`，最多扫描至深度 `5`，累计最多检查 `20,000` 个目录条目。目录浏览列表也可能因条目上限被截断；结果中的截断提示表示尚有未扫描或未显示内容，可选择更具体的子目录继续。

递归扫描不进入隐藏子目录，不跟随子目录符号链接，并跳过素材、历史与环境子目录：

```text
workbench references output review revisions history
archive archives backup backups frames images rgb
undistorted renders media assets node_modules venv env
core_env vision_env __pycache__ site-packages
```

这些目录中约定的实例标识与素材路径仍可被读取；跳过的是把子目录当成更多独立实例的递归搜索。`ready:false` 的实例会列为跳过，扫描上限会标记 `truncated` 并说明边界。

导入结果包含新增、跳过与失败项及各自原因；界面显示数量、失败详情和扫描边界提醒。单个实例失败不撤销其他已成功实例。重复导入同一来源目录复用已有场景，不覆盖它的工作台数据或反馈；相同请求重试也复用已成功的来源。结果未确认时可重试原请求，修复失败素材后也可再次导入。

## 卸载与重新导入

导入或新建场景的列表行提供「卸载」，在同一行确认后从列表移除并卸载运行中的工作台，包括暂时不可用的场景。默认场景不可卸载；卸载当前场景前保存提示草稿，随后返回默认场景。卸载状态在服务重启后保留。

卸载保留来源 GLB、`.blend`、参考素材、已修改的工作台结果、会话、反馈与 Codex 任务，不删除文件或取消任务。执行中的回合、尚未解决的送达核实或进行中的操作会阻止卸载并显示具体原因，先完成处理后可重试。

再次导入同一个来源目录会重新显示原场景，并复用原项目、会话、已修改模型与参考数据，不从来源重建初始状态、不覆盖编辑成果，也不创建新 Codex 任务。

## 接口摘要

| 接口 | 请求 | 返回要点 |
| --- | --- | --- |
| `POST /api/projects/folders` | 可选绝对目录 `path` | 当前 `path`、`parent`、子目录 `directories` 与 `truncated` |
| `POST /api/projects/check-folder` | 绝对目录 `path`；使用当前工作台的浏览器访问权限 | 只读检查报告：`status`、`can_import`、`counters`、`instances[].checks`／`metrics`、`skipped`、`errors` 与 `truncated` |
| `POST /api/projects/import-folder` | 绝对目录 `path`、32 位小写 UUID hex `request_id` | `projects`、`imported`、`skipped`、`errors`、`truncated`、扫描计数 `counters` 与自动检查报告 `ready_check` |
| `POST /api/workspace/scene-references` | 其他已加载场景的 `project_id`；使用当前场景的浏览器访问权限 | 当前场景拥有的不可变引用记录，包含引用 ID、来源名称与版本、复制的场景 JSON／GLB 及可用首张 GT；反馈通过 `scene_refs` 提交引用和预览 |
| `DELETE /api/projects/<project_id>` | 要卸载的场景 ID；使用当前工作台的浏览器访问权限 | `unloaded_project_id` 与默认场景元数据 `fallback`；同一请求可安全重试，受保护或仍有操作的场景返回具体原因 |

同一个 `request_id` 只能用于同一个文件夹，也不能复用新建场景的请求编号。接口结果用于刷新场景列表，不指定或切换当前正在编辑的场景。
