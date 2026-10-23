# 可编辑物理世界原型

这一版在原工作台后增加对象级场景、Blender 构建和 MuJoCo 刚体仿真。工作台仍在运行主机上启动 Codex；工具也在该主机执行。部署在 spark145 时，建模、仿真和渲染均由 spark145 执行，浏览器继续显示 GLB、接收标注与修改意见。

示例是**合成测试场景**，不是照片重建成果。当前实现检验对象编辑、源工程重开、导出一致性、碰撞与关节行为；没有自动恢复真实材质、质量或摩擦，也没有完成实拍相似度评测。合成示例不进入已完成真实场景交付清单。

## 数据与职责

`world/scene.json` 是权威对象记录。`world/history/` 保存历史版本；编辑、恢复均创建新版本。Blender 和 GLB 是按该记录生成的资产，构建目录保留输入、日志和检查报告。不要手工修改派生 GLB 或 Blender 文件后继续把它们当作同一构建结果；需要保留的外部网格应作为项目内 OBJ 资产引用。

历史恢复只恢复场景记录，不恢复外部 OBJ 的旧文件内容。资产更新应使用不同文件路径保留旧版；构建与发布会检查所引用资产的内容哈希。

每个对象有稳定 `id`、父子关系、位置/旋转、外观几何、独立碰撞几何、PBR 材质、质量/摩擦以及证据标签。米、千克、秒、Z 轴向上；对象变换相对父对象，XYZ 欧拉角用弧度。盒子尺寸为全长，球和圆柱用半径，圆柱沿局部 Z 轴。Blender 导出负责转换到 glTF 坐标；物理轨迹保存 Z-up 世界位置与 `wxyz` 四元数。

外观支持盒子、球、圆柱组合和项目内 OBJ；碰撞支持基础形状组合。杯子由底部和分段侧壁组成，空腔保持开放。材质支持不透明 base color（alpha 必须为 1）、roughness、metallic。关节支持 hinge 和 slide，以及局部轴、锚点、限位和阻尼。首次实现没有透明材质、流体、软体、纹理反演或任意网格碰撞。

`evidence` 区分 `measured`、`observed`、`estimated`、`synthetic`。`provenance.sources` 保留真实输入来源；渲染图不能作为实拍观测。MuJoCo 根据碰撞形状和质量估计惯量，这一版不声称恢复了实测重心和惯量。

## 在运行主机准备环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt -r backend/requirements-world.txt
export SCENE_WORLD_BLENDER=/usr/bin/blender
```

当前桥按 Blender 4.0 API 实现；构建用独立后台进程执行，不加载用户启动文件。MuJoCo 是可选依赖，普通工作台不需要它。请求仿真而未安装时明确报错，不以动画替代仿真。

先运行独立示例，所有生成文件放到指定项目里：

```bash
.venv/bin/python scripts/world.py --project /tmp/physical-world-demo init examples/physical_world/scene.json
.venv/bin/python scripts/world.py --project /tmp/physical-world-demo inspect --object-id cup
.venv/bin/python scripts/world.py --project /tmp/physical-world-demo simulate --expected-revision 1
.venv/bin/python scripts/world.py --project /tmp/physical-world-demo build --expected-revision 1 --render
```

`simulate` 返回仿真 `id`、物理检查和文件路径。把该 ID 传给 `build --simulation-id ID`，可生成含一个同步动画的 GLB，在原时间轴播放。回放的数据来自仿真，网页本身没有另跑一套物理引擎。构建超时或失败时查看返回的 `blender.log`，修复后重新构建。

## Agent 编辑闭环

CLI 模式和 MCP Events 模式均提供同一组工具：

| 工具 | 作用 |
| --- | --- |
| `workspace_inspect_world` | 读取世界或指定对象，取得世界版本 |
| `workspace_initialize_world` | 从项目内 JSON 初始化，拒绝覆盖既有世界 |
| `workspace_edit_world` | 一次事务执行对象 add/update/delete |
| `workspace_restore_world` | 恢复旧状态为新版本 |
| `workspace_simulate_world` | 执行重力、冲量、持续力或关节力并检查 |
| `workspace_build_world` | 生成 Blender、GLB、可选固定视角图或仿真动画 |
| `workspace_evaluate_world` | 检查当前构建、来源与可选物理结果 |
| `workspace_publish_world` | 经世界版本和工作台版本检查后发布 |

工作台的 `scene.revision` 与 `world.revision` 分别计数。先通过 `workspace_get_context` 获取当前工作台版本，再用 `workspace_inspect_world` 读取世界和对象 ID。GLB 节点的 `stable_id` 对应世界对象 ID，原有部件引用可以继续使用。

例如移动杯子，先读取实际坐标，再在 X 轴加 0.2 米，调用：

```json
{
  "expected_revision": 1,
  "changes": [
    {"op": "update", "object_id": "cup", "fields": {"position": [0.2, 0.0, 0.8]}}
  ]
}
```

这里的位置只是接口示例，应使用当前场景读出的实际坐标。`fields` 替换完整顶层字段，不执行深层合并；改尺寸时同时提交新的 `visual` 和 `collision`。修改成功会增加世界版本。随后以新版本构建、评估和发布，失败时保留旧预览，修复源记录后再试。

仿真动作示例：

```json
{"type":"impulse","object_id":"cup","vector":[0.05,0,0]}
```

`impulse.vector` 单位为 N·s；`force.vector` 为 N，并指定 `duration_sec`。柜门使用 `{"type":"joint_force","object_id":"door","force":0.2,"duration_sec":0.5}`，铰链单位为 N·m，滑轨为 N。动作作用于仿真副本，不改写编辑状态。

自然语言建议：“读取当前世界，把杯子沿 X 轴移动 20 厘米并换成蓝色；检查其他对象未变，模拟重力，构建并发布新版本。”Agent 使用上述工具完成步骤；已有普通 GLB 项目需要先准备对象记录，不会自动转换成完整物理世界。

## 验收与局限

构建检查 Blender 重开、对象身份和层级、GLB 结构与资产哈希。仿真使用固定步长，输出带版本和时间戳的轨迹，以及非有限状态、接触穿透、关节限位等检查。动画发布要求对应物理检查通过。源文件、世界版本或构建产物改变后，旧构建不能继续通过世界发布入口。

`visual_accuracy` 与 `physical_calibration` 当前明确为 `not_measured`。物理冒烟检查通过仅说明当前动作未触发这些失败，不能代替实物实验或证明所有动作稳定。固定视角渲染供人工检查；下一步应加入真实尺度、多视角留出集和滑动/开合实测记录。

```bash
.venv/bin/python -m unittest discover -s backend/tests -p 'test_world_*.py'
.venv/bin/python scripts/test_world_blender.py --blender /usr/bin/blender --output /tmp/world-blender-check
```

验证在独立项目目录中运行，不访问生产任务。真实场景重建完成并通过验收后，仍按项目统一交付规范发布；稀疏初始化和合成演示不算完成场景。

## 本版验证记录（2026-10-23）

- spark145：Python 3.12、Blender 4.0.2、MuJoCo 3.17.0。实际完成固定视角渲染、移动/旋转/换色、OBJ 坐标与缩放、Blender 重开和 GLB 身份/层级检查。
- 真实 Three.js 工作台播放一个 2 秒动画；在 0、1、2 秒核对全部 7 个对象的世界姿态与 MuJoCo 轨迹，位置误差小于 0.1 毫米，无页面异常。
- spark 上真实 Codex Agent 经 MCP 读取、编辑、仿真、构建、评估并发布：仅将 `block` 的 X 增加 0.1 米，其他源字段不变，世界和显示场景版本均从 1 到 2；物理检查通过，动画为 61 个采样点、2 秒。
- 新增世界模型测试 35 项、适配器测试 7 项、插件打包测试 11 项通过。全量后端 466 项中 450 项通过；13 项失败、3 项错误均与改动前基线相同，涉及旧反馈摘要/图像数量断言及旧 FakeAdapter 参数，不计作本功能通过的测试。

验证的是合成场景闭环。照片级重建质量与物理实测参数仍未评估；下一阶段应选定一个真实桌面场景，采集尺度和多视角照片，先验证外观与对象编辑，再用可重复实物动作校准质量、摩擦和关节。
