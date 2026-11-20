# Robo 硬件 Harness 本地运行手册

## 当前已经能做什么

Robo 现在包含一条可在 Apple Silicon Mac 上运行的硬件开发闭环：

```text
Robo Agent
  -> robo_run_policy（模型生成受限 Python 策略）
  -> observe / Manifest 动作 / verify 闭环
  -> HardwareRuntime（租约、安全仲裁、停止、Trace）
  -> DeterministicSimulatorAdapter 或厂商 MCP Adapter
```

Textual 界面会把五个模型可见的 `robo_*` 工具渲染成硬件专用卡片，直接显示
设备状态、策略源码及文件路径、逐步 observation/action/verification 输出、遥测、
故障、证据路径、命令回执和验证结果。还提供 CaP-X Agent
Bridge：

```text
CaP-X Runner
  -> POST /chat/completions
  -> Robo App Server 会话
  -> 模型生成 Python
  -> CaP-X 隔离执行并反馈 stdout/stderr/图像
```

Mac 本地可以完整验证协议、Agent 多轮修复和确定性设备行为。官方 CaP-X 全量场景仍需要 Linux x86_64/NVIDIA CUDA。

## 一、本地启动 Robo

### 选择机器人，再下达任务

已有环境可直接运行：

```bash
uv run robo --legacy-harness --workdir .
```

空闲时输入 `/robot`，选择已配置并发现的机器人，点击「连接并读取」。面板显示
机器人声明的动作、独立验证器、当前环境和最新观测；有本机 RGB 证据时，可以
点击「打开画面」。点击「返回任务」后，用自然语言下达任务，所选设备 ID
会进入当前会话的任务上下文。连接本身不获取租约、不武装、不发送运动动作。
重复读取已连接的机器人只更新观测，不重新连接或重置场景。

按 Esc 或「返回任务」关闭面板。发现/连接进行中按钮显示「取消并返回」，会通过
带操作 ID 的服务端取消请求，确认本次检查已结束后返回；不是只关闭窗口。
若取消未确认，面板保留并显示提示，可再次尝试。取消不会解除已有设备的连接、
租约或武装状态；设备紧急停止仍使用原来的独立停止入口。

发现失败保留上次选择，并标注「本次未确认」，不把查询失败当成用户取消选择。
`/clear` 清空会话选择；之后明确连接同一设备也会重新传入任务上下文。

先选择机器人，它的实际环境由观测和设备声明确定。Lift/Stack 是仿真环境设置，
不是机器人选择项。当前面板展示已配置的 Viewer/录像状态，不在这里切换它们
或仿真任务。`sim-arm-1` 是协议模拟器，MuJoCo Panda 则是物理仿真机器人。
新设备仍通过已有 MCP 配置接入；面板不自动扫描网络或连接未知物理设备。

启动检查只核对模型配置及凭据是否可用，明确标注未发请求验证模型连通性；
机器人发现或连接失败会显示错误类别。任务运行中不能进入选择流程。
目前需要 Legacy Harness，Unified backend 会明确报告不支持。

在仓库根目录执行：

```bash
uv sync --all-extras
uv run robo --workdir .
```

可以从一个可验证的小任务开始：

```text
列出可用 Robo 设备，连接 sim-arm-1，获取租约并显式武装，
生成一段 Python 策略，用 fold_cloth 折叠 shirt；策略内先观察、执行动作、
再次观察并用 cloth_folded 验证，最后停止设备。
```

模型可使用以下工具：

- `robo_devices`：发现、描述、连接、获取租约、武装、解除武装和释放租约；
- `robo_observe`：读取设备状态、当前 run trace 和 Git 工作区状态；
- `robo_run_policy`：执行模型生成的受限 Python 闭环策略，默认需要批准；
- `robo_verify`：策略外诊断使用的只读任务验证；正常任务应在策略内调用
  `verify()`；
- `robo_stop`：独立请求停止并检查 Adapter ACK，始终允许调用。

`robo_execute` 和 `robo_sequence` 仍保留为内部兼容类，但不会出现在模型工具
列表里，所以模型不能再通过多轮单步 Tool Call 假装执行 Code-as-Policy。

策略示例：

```python
before = observe()
fold_cloth(cloth="shirt")
after = observe()
report = verify("cloth_folded", cloth="shirt")
print(report["status"])
```

允许变量、下标、算术/比较、`if` 和有界循环；禁止 import、属性访问、函数/
类定义、文件、网络和进程访问。整棵 AST 会在任何动作前完成校验。运行错误、
取消或超时会请求停止设备，并向模型返回错误码、源码 URI 和行列，让模型先按
当前物理状态重新观察，再生成修正版策略。

确定性模拟器设备 ID 是 `sim-arm-1`。它不是物理仿真器，不模拟碰撞、布料动力学或相机，只用于验证 Harness 的控制语义。

硬件 Trace 默认保存在当前会话目录的 `hardware-runs/<run_id>.jsonl`，策略源码
保存在 `hardware-runs/policies/<policy_id>.py`。关闭会话日志时，二者保存在项目的
`.vibe/hardware-runs/<session_id>/` 下。模型通过 `robo_observe` 最多只会看到最新
200 条事件，原始 JSONL 不会全部灌入上下文。

在默认 Legacy Harness 中，任何时候都可以按 `Ctrl+Shift+X` 请求全局停止。
Robo 会先向所有已连接或正在连接的设备发送停止，再中断当前 Agent turn；界面
只有在 Adapter 返回 ACK 后才显示已确认停止。`--experimental-harness` 使用的
Unified backend 目前还没有 Python `HardwareRuntime`，因此会明确返回
`NOT_IMPLEMENTED`，不会伪装成停止成功。

### 启用有画面的 MuJoCo Panda

安装可选的 Robosuite 仿真依赖：

```bash
uv sync --extra simulator
```

在项目 `.vibe/config.toml` 中加入：

```toml
[[mcp_servers]]
name = "robo-mujoco"
transport = "stdio"
command = "uv"
args = ["run", "--extra", "simulator", "robo-sim-mcp"]
sampling_enabled = false
startup_timeout_sec = 60.0
tool_timeout_sec = 120.0
disabled_tools = [
  "robo_devices",
  "robo_connect",
  "robo_arm",
  "robo_disarm",
  "robo_observe",
  "robo_verify",
  "robo_execute",
  "robo_stop",
  "robo_disconnect",
]
```

重启 Robo 后，让它连接 `mujoco-panda-1`。首次连接会打开由 Robo 管理的
独立 MuJoCo 原生 Viewer 进程；`move_cartesian`、`gripper_open` 和
`gripper_close` 会实时驱动画面。MuJoCo 仿真仍由 MCP 进程唯一持有，Viewer
只加载只读模型副本，并通过本机 mmap 读取最新关节与 mocap 状态，不拥有控制接口。
这样既避开了 macOS 的 GUI 主线程问题，也保留了 MuJoCo 原生相机交互。

Viewer 默认从斜侧面显示完整 Panda、工作台和目标物体。窗口可以任意放大；按住
鼠标左键拖动旋转视角，按住鼠标右键拖动平移，滚轮缩放。双击几何体可以把相机
焦点切换到该物体。

关闭窗口只会关闭 Viewer，机械臂控制继续运行；断开设备或退出 Robo 会自动回收
Viewer、只读模型副本和临时状态缓冲。Viewer 异常退出也不会中断策略、证据或录像。

```text
连接 mujoco-panda-1，获取租约并武装。先回到 home，
再沿 X 正方向移动 0.2、持续 20 步，闭合夹爪，观察状态，最后停止。
```

如果只想做无窗口的协议测试，在 MCP Server 配置中增加：

```toml
[mcp_servers.env]
ROBO_SIM_HEADLESS = "1"
```

无论是否显示预览窗口，配置成 Robo 硬件 Adapter 后都会自动录制原生 MP4。
`robo_stop`、断开连接或关闭会话会结束并落盘当前录像；随后调用
`robo_observe`，可在 `snapshot.state.recording_uri` 看到文件 URI。默认目录为：

```text
<会话目录>/hardware-runs/recordings/robo-mujoco/*.mp4
<会话目录>/hardware-runs/evidence/robo-mujoco/*.png
```

关闭会话日志时，对应目录在：

```text
<项目>/.vibe/hardware-runs/<session_id>/recordings/robo-mujoco/*.mp4
<项目>/.vibe/hardware-runs/<session_id>/evidence/robo-mujoco/*.png
```

直接单独启动 `robo-sim-mcp` 时，也可以用 `ROBO_RECORDING_DIR` 和
`ROBO_EVIDENCE_DIR` 指定这两个目录。RGB 证据帧和录像共用同一个离屏相机上下文，
证据与视频方向已经统一为正向；原生 Viewer 使用独立的只读状态镜像渲染。

## 二、安全停止、释放和重连

正常结束或故障恢复按下面的顺序操作：

```text
robo_stop(device_id)
-> robo_devices(action="release", device_id=..., lease_id=原租约)
-> robo_devices(action="connect", device_id=...)
-> robo_devices(action="acquire", device_id=...)
-> robo_devices(action="arm", device_id=..., lease_id=新租约)
```

- `stop` 会撤销武装状态并保留原租约 ID，保证随后 `release` 能完成清理；
- 同一租约的 `release` 可以安全重试，只记录一次释放事件；
- 已武装或仍持有活动租约时直接重连会被拒绝；
- 完成停止与释放后，重连会先断开旧 Adapter 连接，再建立新连接；
- 重连后必须获取新租约并重新武装，旧租约不能继续控制设备。

## 三、运行本地自动化测试

运行 Robo 硬件 Runtime、MCP Adapter、工具和 CaP-X Bridge 的聚焦测试：

```bash
uv run pytest \
  tests/core/hardware \
  tests/core/tools/builtins/test_robo.py \
  tests/app_server/test_tool_projection.py \
  tests/capx
```

其中 `tests/core/hardware/test_policy.py` 验证代码策略闭环、先校验后运动、循环
上限、错误定位和失败停止；Robo Tool 测试验证策略源码会保存到会话目录。

其中本地迷你 Bench 会模拟这个多轮场景：第一次代码引用不存在的变量并收到 stderr；第二次读取旧代码和错误后改成 `fold_cloth("shirt")`，最终完成任务。它使用白名单 AST 解释器，不在 Robo 进程内执行任意模型代码。

## 四、启动 CaP-X Agent Bridge

先确保 Robo 的模型 Provider 和 API Key 已按主 README 配置。Bridge 自己的访问令牌是可选的 `ROBO_CAPX_API_KEY`，它不是模型 Key。

终端一：

```bash
uv run robo-capx-bridge -C . --host 127.0.0.1 --port 8110 --trust
```

健康检查：

```bash
curl http://127.0.0.1:8110/health
```

终端二运行本地 CaP-X 风格烟雾测试：

```bash
uv run robo-capx-smoke \
  --url http://127.0.0.1:8110/chat/completions
```

Bridge 支持 CaP-X 使用的文本消息、`data:image/...;base64,...` 图片和多轮历史，响应字段为 `choices[0].message.content`。每次请求返回 `x-robo-run-id`，Trace 默认位于 `.vibe/capx-runs/`。

如果设置了 Bridge 访问令牌：

```bash
export ROBO_CAPX_API_KEY='自行填写，不要提交到仓库'
```

CaP-X 客户端需要发送 `Authorization: Bearer <token>`。

## 五、接入不同型号机械臂

第一版支持本地 stdio MCP Adapter。厂商 Server 需要实现以下九个 Tool：

- `robo_devices`
- `robo_connect`
- `robo_arm`
- `robo_disarm`
- `robo_observe`
- `robo_verify`
- `robo_execute`
- `robo_stop`
- `robo_disconnect`

在项目 `.vibe/config.toml` 中配置：

```toml
[[mcp_servers]]
name = "robo-ur5"
transport = "stdio"
command = "uv"
args = ["run", "--project", "/path/to/vendor-adapter", "vendor-robo-mcp"]
sampling_enabled = false
disabled_tools = [
  "robo_devices",
  "robo_connect",
  "robo_arm",
  "robo_disarm",
  "robo_observe",
  "robo_verify",
  "robo_execute",
  "robo_stop",
  "robo_disconnect",
]
```

`name` 必须以 `robo-` 开头。九个原始 Tool 必须全部放入 `disabled_tools`，否则 Robo 会拒绝把该 Server 注册成硬件 Adapter。这是为了防止模型绕过 `HardwareRuntime` 的租约、武装、审批、追踪、任务验证和停止语义直接调用厂商接口。

Server 返回的 `DeviceCapabilityManifest` 必须声明设备 ID、厂商、型号、设备类型和支持动作；关节数、单位、坐标系、固件和安全限制放在 `metadata`。MCP 负责高层发现与调用，不负责伺服、看门狗或物理急停。

每个 `VerificationCapability` 还应通过 `applicable_actions` 声明它验证哪些动作，并用 `parameter_bindings` 声明“验证参数名 -> 动作参数名”的映射。Runtime 只会用动作完成之后、映射参数一致且 `snapshot_sequence` 更新的报告解除验证门禁；旧帧或无关条件不会被当成该动作的后置条件。

## 六、接入官方 CaP-X Bench

在 Linux x86_64/NVIDIA 机器上启动 Robo Bridge，然后在 CaP-X 仓库运行最小 S1 trial：

```bash
uv run --no-sync --active capx/envs/launch.py \
  --config-path env_configs/cube_lifting/franka_robosuite_cube_lifting_privileged.yaml \
  --server-url http://<robo-host>:8110/chat/completions \
  --model robo-harness \
  --total-trials 1 \
  --num-workers 1 \
  --record-video False
```

先用 `--use-oracle-code True` 验证 CaP-X 环境自身，再去掉该参数验证 `prompt -> Robo -> Python -> simulator -> reward`。CaP-X 的生成代码必须在独立 worker/容器内执行，不能放入拥有真实硬件权限的 Robo 进程。

## 七、本机 Robosuite 探测结果

2026-09-17 在 Apple M2 Max/macOS 上进行了以下探测：

- CaP-X 官方源码的 `query_model()` 已成功请求 Robo Bridge 的 `/chat/completions` 并读取 `choices[0].message.content`。
- PyPI `robosuite==1.5.2` 搭配默认 MuJoCo 会在 `setup_references()` 触发 joint 类型断言；固定 `mujoco==3.3.7` 后，原生 `Lift/Panda` 已完成 `reset + step`。
- 使用同一兼容组合时，CaP-X 源码的 `FrankaRobosuiteCubeLiftLowLevel` 已完成初始化和 `reset`。它的运动路径调用了定制 fork 才有的 `step(..., skip_render_images=True)`，因此 PyPI Robosuite 不能完成 CaP-X 动作循环。
- CaP-X 固定 Robosuite fork 的 Git 请求持续超时；GitHub tarball 下载到约 281 MiB 后中断且校验损坏，未能安装固定 fork。

所以当前验收结论是：Robo 的本地 HardwareRuntime、CaP-X 协议接入、Robosuite 原生物理步进和 CaP-X 包装器 reset 已通过；CaP-X 的定制运动循环与官方完整 Benchmark 尚未通过。完整研究与外部验收矩阵见 [CaP-X 接入研究](research/capx-bench-integration.md)。

## 八、实验性 RGB-D Lift / H1

H1 固定哈希资产、视觉观测模式、开关、配对 smoke 和局限见
[Mac RGB-D Lift / H1 接入](robo-h1-lift-runbook.md)。


## 任务进度与结果面板

交互式 CLI 在第一次硬件工具事件后，会在输入框上方显示固定面板。
无需新配置；模型、策略预算、控制器、H1 默认值均不变。

- 执行时显示当前观察、移动、开闭爪或停止事件；工具之间显示模型正在生成下一步。
  进度来自已有运行事件，不猜测“接近/下降/抬升”的意图，也不显示虚假的百分比。
- 控制结果显示最近动作的到位判定、结束原因、最大轴误差（毫米）和实际周期数。
  没有运动判定的普通动作回执不会被标成“已到位”。
- 任务结果仅使用独立验证回执；执行后会标记待重新验证。停止 ACK 和租约释放单独显示，
  自主收尾仅使用当前轮设备工具回执。系统兜底另列停止 ACK、租约结果与独立验证，
  不把兜底结果算作自主全链路成功；没有租约显示“无租约，无需释放”。
- 点击“验证 / 失败详情”查看最近失败和验证检查；点击“打开录像”“策略源码”或
  “最近运动轨迹”用系统应用打开本地文件。录像未封口时可能暂不可播放；文件缺失时显示提示。
  macOS 源码和 JSON 使用文本编辑器打开，不执行源码。远端文件不自动下载。
- 工具原始结果默认可折叠，展开后仍可查看策略和完整回执。新一轮任务清空面板，
  既有结果仍在会话历史与原实验目录中。多设备时显示最近设备，不合并不同设备的成功状态。

轮次失败（包括生成截断）进入“验证 / 失败详情”，与工具失败分别展示。
兜底超时、停止或释放失败会标明失败/未确认；旧记录缺少兜底字段时不猜测结果。
同一会话的状态快照恢复会重建这些信息，新轮次清空；重启后的旧会话仍以原始审计文件为准。

取消与紧急停止沿用原来的按键和运行时路径。进度转发不重试策略、不额外观察设备，
也不把进度行加入模型工具结果。UI 使用公开 app-server 事件；没有新增配置字段，
不需要修改 Settings Manager 或 Le Chat 的配置白名单。
