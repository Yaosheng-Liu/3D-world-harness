# Scene Feedback 插件与 MCP Events

仓库现在也是一个 Agent Plugins 包：根目录 `plugin.json` 提供身份，
`mcp.json` 连接项目专用的 MCP，`skills/visual-feedback/` 提供审图流程。
插件负责参考图、3D 场景、标注、动态机位和外部二维人体结果的查看与引用。
「打开并导入」会自动检查模型、参考素材、相机与帧时间；失败实例报错，其他有效实例继续导入，有使用限制的实例导入并显示提示。插件内的 [`workbench-ready-check` skill](../skills/workbench-ready-check/SKILL.md) 供 agent 在交付前自查，使用命令行 `scripts/check_workbench_ready.py --json` 获取只读报告，不创建工作台数据或 Codex 任务。仅模型实例与未附相机的参考仍可导入并手动比较，已声明但损坏的素材不能通过。检查范围与限制见[导入前检查](folder-import.md#导入前检查-ready)。
人体追踪推理与重建位于独立的 [capsule skill](../external-skills/capsule-human-tracking/SKILL.md)，
不随工作台插件安装。
默认 skill 使用 WholeBody133。左图工具栏「关键点」可编辑原帧已声明的身体、脚、脸和手部关键点；本轮修改按帧累加为「关键点修改N」卡片，可自动插入提示或用 `@` 引用。原图、修正前后坐标与来源 JSON 随反馈提交；插件仅负责编辑与证据交换，不包含二维推理、三角化或 Blender 重建运行环境。

原生输入框正文和聊天记录使用图标加编号：物体 `🧊1`、部件 `🧩1`、标记 `📍1`、图片 `🖼️1`、人体 `🧍1`、修正 `✏️1`、时间 `🕒1`、场景参考 `🏞️1`。圆角来源卡片提供完整来源预览、名称与精确来源详情，并支持移除；各类独立编号，同源复用，旧草稿自动迁移且保持精确绑定。聊天记录显示图标，归档数据仍保存协议 token 与准确的来源、图片、相机、机位、帧时间和场景版本。

左侧场景行的引用按钮可只读捕获同一工作台已加载的其他可用场景，保留版本、场景 JSON、完整 GLB 副本、可用模型预览与首张 GT 图，显示为 `🏞️1`。模型预览在后台生成；服务确认未变的同一快照可在新一轮或刷新后复用已有预览。预览不可用时可使用已有 GT，二者都不可用则明确报错，不加入引用。不改变当前任务绑定，不启动来源任务，也不复制其会话文字或凭证；来源更新或卸载后，引用副本仍可用。场景引用通过 `scene_refs` 与 `[[scene:id]]` 绑定，模型摘要只说明名称、版本与归档路径并提供预览／GT，完整几何按需读取。每轮最多 4 个场景，GLB 复制预算为单场景 200 MiB、单反馈 400 MiB。只在当前工作台内选场景，`@` 候选范围保持原样，详见[场景参考说明](folder-import.md#引用其他场景作为参考)。

模型收到的文本使用简洁摘要，保留原用户文字、ID、来源精确时间、机位、`frame_index` 和版本，来源元数据只说明一次。关键点摘要只列改动关节的修改前／后值；完整关键点、相机矩阵和旧证据仍保存在 `state.json` 与原始修正档案中。MCP 首段文本与 `structuredContent` 默认都返回同一精简摘要，保留 `feedback_id`、完整 `state_path` 和来源归档指针。`workspace_get_feedback(feedback_id, include_details=False)` 默认读取摘要；传入 `include_details=True` 按需读取完整归档字段。两种方式的图片都只包含本轮相关来源，其他原图可按归档路径读取；stdio 与 Events RPC 使用同一参数。

发送给模型的图片按本轮引用、实际标记及必要当前画面选取，未显式引用的本轮标记仍保留。没有标记且没有选中高亮的普通标注副本会省略；相同文件内容只发送一次，来源别名仍可定位原证据。完整反馈包与原图不因此删除或改写。

## 两个可以分别验证的步骤

**本地插件安装**把工具和技能加入 Codex；它不需要公共网络，也不会自动注册
ChatGPT 连接。配置中的项目 ID、项目目录和数据目录必须匹配正在运行的工作台。
桥接程序在每次连接时检查项目身份，读取该数据目录的控制令牌，发现错误配置便停止。
插件包不包含控制令牌、Codex 凭据、参考素材、模型权重或工作台任务数据。

**MCP Events 投递**由支持该协议的宿主创建订阅，提供 callback URL 与签名密钥。
用户提交后，服务器发送反馈 ID 与摘要；模型再用 `workspace_get_feedback` 取回本轮相关图像
与摘要，需要完整字段时传入 `include_details=True`。只有宿主订阅、回调验证和真实事件触发都已通过，才能确认后续任务会继续。
本地收件器收到 webhook 的 `2xx` 不能证明某个 Codex 对话已经收到或执行。

官方 [MCP Events 文档](https://developers.openai.com/plugins/build/mcp-events)
当前明确提供的是 ChatGPT 的 MCP 2.0（`2026-07-28`）webhook 集成。
本机 Codex CLI 0.159.2 的插件可用，但 MCP 2.0 仍在实验开关下；它的
`mcpServer/event/stream/*` 接口不代表所有本地插件都能唤醒现有任务。
因此已有 Codex 投递方式仍可继续使用，事件模式需要支持它的宿主。

## 从侧边栏上传压缩包

`0.1.0` 与 `0.1.1` 只覆盖了本地加载验证。进一步调用 Codex 桌面后端的
私有插件新建接口时，旧包返回 HTTP 400：`Expected a single plugin archive`。
旧包把仓库的 `.agents/plugins/marketplace.json` 一起装入了单插件归档；这个清单
属于本地市场发现配置，上传包不应携带。`0.1.2` 从分发包中排除市场清单，同时
保留完整 MCP、技能、详细介绍、图标，以及 `.codex-plugin/plugin.json` 和
`.mcp.json` 兼容文件。仓库及配置出的本地安装目录仍可使用本地市场清单。

请使用 `0.1.64` ZIP。如果界面只提示「无法添加插件」，应保留具体校验错误或
上传请求的响应；本地 `plugin/read` 成功不能代替云端归档导入验证。
官方 [包结构说明](https://developers.openai.com/plugins/build/plugins) 区分了
单插件分发包和本地市场目录；[上传错误说明](https://developers.openai.com/plugins/deploy/submission-errors)
中的公共目录上架要求也不能直接作为个人侧边栏错误的原因。

上传成功与 MCP 可运行是两个验收步骤。原始 ZIP 中的本地 stdio MCP 没有默认
项目；安装环境需要读取实际工作台的数据目录并按下文配置。若宿主运行在云端，
它无法用云端自己的 `127.0.0.1` 连接这台电脑的工作台；需要可访问的 MCP 服务
及真实连接设置。不要将控制令牌放进上传包，也不要把移除 MCP 的技能包当作
已经接入视觉反馈工具的版本。

需要在 ChatGPT 注册 MCP 连接时，使用「创建 MCP 应用」并提供可访问的 HTTPS
端点或 Secure MCP Tunnel，见 [连接与测试](https://developers.openai.com/plugins/deploy/connect-chatgpt)。
公共目录上架另有 [提交流程](https://developers.openai.com/plugins/deploy/submission)。
个人侧边栏安装、本地市场安装、公共目录上架和事件回调应分别验收。

## 安装到本地 Codex

先按下文启动独立的 Events 试验服务；原有服务配置见 [README](../README.md)。安装插件的机器必须能读取这份工作台
数据目录；本地 stdio 桥接默认连接 loopback HTTP 服务。项目 ID 可从工作台
项目链接 `/p/<project_id>/` 或该项目的 `/api/health` 响应获取。

在仓库根目录生成一个项目专用的安装目录，替换下面的 ID 和绝对路径：

```bash
python3 scripts/configure_plugin.py \
  --project-id YOUR_32_CHARACTER_PROJECT_ID \
  --project-dir /absolute/path/to/reconstruction \
  --data-dir /absolute/path/to/workbench-data \
  --port 18769 \
  --output dist/local-plugin

codex plugin marketplace add ./dist/local-plugin
codex plugin add scene-feedback-harness@scene-feedback-local
```

每个安装目录只绑定一个项目。切换项目时重新生成到新的空目录，再更新 marketplace；
不要复制另一场景的控制令牌或更改其目录来绕过身份检查。生成目录的 `mcp.json`
只保存项目设置，因此 Codex Desktop 从应用启动时也能找到同一项目。
仓库原始 `mcp.json` 不设置默认项目；直接安装时需给进程提供
`SCENE_FEEDBACK_PROJECT_ID`、`SCENE_FEEDBACK_PROJECT_DIR`、
`SCENE_FEEDBACK_DATA_DIR`、`SCENE_FEEDBACK_PORT`。

安装后新开一个任务，调用插件的 `workspace_get_context` 核对场景，再说
「进入人工调试模式」。事件模式的工作台请求立即返回；订阅与后续唤醒由宿主负责。
没有事件宿主时，请继续使用现有工作台配置的 Codex 投递流程。

可选 `--mcp-url` 必须是同一项目的 `/p/<project_id>/mcp`，使用 HTTPS
或 loopback HTTP。portable `mcp.json` 的远端 HTTP 连接不能直接使用未加密的
内网 IP；这里使用 stdio 桥接，并在桥接本身保持同样的地址规则。

## 生成可分发 ZIP

```bash
python3 scripts/build_plugin.py --output dist/scene-feedback-harness.zip
# 带版本号的本次分发包：
python3 scripts/build_plugin.py --output dist/scene-feedback-harness-0.1.64.zip
# 或打包已配置的本地版本：
python3 scripts/build_plugin.py --source dist/local-plugin --output dist/project-plugin.zip
```

ZIP 内只有一个 `scene-feedback-harness/` 目录，包含插件身份、技能、桥接程序、
工作台运行源文件和说明。文件排序与时间戳固定；不打包 `.venv`、`.git`、测试、
素材、工作台数据或本地 marketplace 清单。配置脚本单独生成供本地安装使用的
清单，重建该安装目录的 ZIP 时仍会排除它。接收机器仍需安装 Python 依赖并启动工作台服务。
推理 worker、推理依赖清单和独立 capsule skill 不进入此 ZIP；重建任务按 skill 配置自己的推理环境。
打包不会替用户托管或注册服务。

## 接入支持 Events 的宿主

先用独立数据目录启动试验服务，避免把现有待发送队列切到另一种宿主：

```bash
.venv/bin/python backend/server.py \
  --mcp-events --port 18769 \
  --project-dir /absolute/path/to/reconstruction \
  --data-dir /absolute/path/to/new-events-data
```

`--mcp-events` 使用外部审图模式，初始不创建 Codex adapter 或任务；新导入场景默认通过事件订阅投递。选中导入场景后可打开「任务」，明确连接服务主机同用户的现有 Codex Desktop 任务，或为当前场景新建会话。服务会发现唯一本机 Desktop 连接，不要求先绑定一个种子任务；失败时保留场景并显示原因。协议背景见 [Codex App Server](https://learn.chatgpt.com/docs/app-server)。

连接成功后，这个场景保存直接投递路线与原 Events 来源，后续图文反馈只送入所选对话；Events 工具及历史仍可读取。已有事件与 outbox 不因连接而重新回放或转送，新反馈不会双重投递；其他场景不受影响。模型、参考图、相机、标记与草稿保留，绑定在重启及卸载后重新导入时恢复。空白首页只作入口，须先选择导入场景；Events 服务的「新建场景」页仍未启用。

默认启动方式继续使用原投递方式，现有队列不会自动迁移。需要局域网访问时可沿用 README 的监听地址与公开地址配置：
`--lan-access open` 可直接访问，默认 `link` 模式使用授权链接。浏览器 LAN 模式不改变 MCP 认证；
云端 MCP 入口仍需公共 HTTPS。

服务的 MCP 入口是 `/p/<project_id>/mcp`，工具和事件方法使用同一个经过认证的
入口（Bearer 控制令牌）。宿主通过 `server/discover`、`events/list` 发现能力，通过
`events/subscribe` 提交项目过滤条件和 webhook delivery，并通过
`events/unsubscribe` 停止。事件名为 `visual_feedback.submitted`，只发送匹配项目的
提交事件；图像由读取工具返回。首次尚无订阅的反馈会保存，验证订阅后尝试投递；
曾经分配、送达、取消或失败的历史事件不会因新订阅或重启自动回放。
订阅默认有效 24 小时、最长 7 天；宿主按 `refreshBefore` 续订。临时错误最多
尝试 8 次，失败后证据仍可按反馈 ID 读取。将用户的实际修改要求写在提交中，在宿主订阅时说明
“收到反馈后读取图像、继续修改当前重建并发布场景”。

ChatGPT 云端需要能访问 MCP 的公共 HTTPS 地址。内网网页能打开不代表云端能连接：
可使用你自己的 HTTPS 反向代理或受控 tunnel，并配置认证、资源授权和可信来源。
仓库不会自动把现有内网工作台公开到互联网。callback URL 由宿主提供；生产回调
只能使用公共 HTTPS，验证和投递都检查地址，禁止跳转到本机或内网地址。

按官方 [连接与测试流程](https://developers.openai.com/plugins/quickstart)
在 ChatGPT Developer mode 注册 MCP 后，会得到 `plugin_asdk_app_...` 技术 ID。
已有该 ID 时可以生成引用它的插件：

```bash
python3 scripts/configure_plugin.py \
  --app-id plugin_asdk_app_YOUR_REGISTERED_ID \
  --output dist/hosted-plugin
```

这个命令只写 `.app.json` 和 manifest 映射；不会注册连接、登录账户或发布插件。
hosted 包不再同时加载本地 stdio MCP，以免同名工具连接两个目标。
`.app.json` 引用包用于已有连接的个人或工作区安装；当前公共目录上传流程
要求提交 MCP 服务声明并完成连接，不接收这种已有应用引用作为目录发布包。
公网 HTTPS、注册 ID、真实宿主订阅和公共目录审核均需在对应平台完成。
本仓库提供本地 marketplace 和可分发文件，尚未声称进入通用插件目录。

完整验收需要确认：宿主发现事件、创建订阅、完成 challenge 验证、收到匹配事件、
调用读取工具获得图片、按用户指示执行，以及取消订阅后停止。还应确认重启、重复
提交和重试不会重复编辑。协议和包结构依据官方
[MCP Events](https://developers.openai.com/plugins/build/mcp-events) 与
[Package your plugin](https://developers.openai.com/plugins/build/plugins) 文档。

## 本地验证

```bash
.venv/bin/python -m unittest discover -s backend/tests -q
python3 scripts/test_plugin_package.py
python3 scripts/build_plugin.py --output dist/scene-feedback-harness.zip
```

后端测试使用临时项目与注入的收件器，验证签名、订阅、重试、取消、重启恢复、
项目授权、完整图片读取和 stdio 桥接；不向现有 Codex 任务发送消息。
当前已验证本地 Codex 能读取插件包。真实 ChatGPT 注册、公共回调和现有 Codex
对话唤醒需要用户的连接信息，尚未在此版本中实际验收。
