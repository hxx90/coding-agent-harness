# Design Proposal: Robo 独立仿真 Viewer 进程

**Tracking issue:** Not created

---

## 1. Background

Robo 的 MuJoCo Panda 适配器同时承担仿真、RGB 证据、录像和预览。
用户需要在一个终端运行 Harness，并在独立窗口持续观察虚拟机械臂。

现有 macOS 路径会在启用预览时通过 `mjpython` 重启 MCP Server。运行时复现表明，
`robo_connect` 创建 Robosuite 离屏渲染上下文时抛出：

```text
NSInternalInconsistencyException:
NSWindow should only be instantiated on the main thread!
```

关闭预览后，同一模型、工具配置和 MCP Server 能在约两秒内成功连接。

## 2. Problem

当前 MCP Server、MuJoCo 离屏渲染和预览窗口位于同一进程。`mjpython` 将 Python
代码运行在线程中，而 Robosuite 创建的 GLFW 上下文要求 macOS 主线程，导致设备连接时
整个 MCP 进程退出。Harness 只能得到被折叠后的 `adapter_error`，用户则误以为模型无法
连接工具。

单纯关闭预览可以恢复控制，但不满足实时观察机械臂的要求。

## 3. Goals

- Harness 与 `robo-mujoco` MCP 保持现有 stdio 工具接口。
- macOS 上连接 `mujoco-panda-1` 时自动打开独立的 MuJoCo 原生 Viewer 进程。
- MCP 进程拥有唯一仿真状态；Viewer 只同步只读状态，不能发控制命令。
- Viewer 默认展示完整机械臂，并支持原生旋转、平移和缩放。
- Viewer 关闭或崩溃不影响控制、证据、验证和录像。
- 断开设备或 MCP 退出时回收 Viewer、模型副本和状态缓冲区。
- headless 模式继续完全禁止 Viewer。

## 4. Non-Goals

- 不增加远程视频流或浏览器 Viewer。
- 不改变模型可见的 Robo 工具、租约、武装、停止或验证语义。
- 不把 Viewer 变成第二个仿真状态所有者。
- 不保证 Viewer 展示仿真器没有生成过的新帧；静止时保留最后一帧。

## 5. Terminology and Constraints

- **Simulator MCP**：`robo-sim-mcp` stdio 进程，唯一拥有 Robosuite 环境。
- **Viewer process**：由适配器自动启动、使用 MuJoCo 原生 GUI 的只读子进程。
- **State buffer**：本机临时 mmap 文件，包含版本化头部和最新关节、mocap 状态。
- **Model mirror**：MCP 启动 Viewer 时导出的临时 MJB 模型；Viewer 只能读取它。
- Viewer 默认自动启动；用户不需要运行第二条命令。
- `ROBO_SIM_HEADLESS=1` 保留为显式关闭 Viewer 的兼容开关。
- 状态传输不经过模型上下文，不改变 evidence/recording 的持久化格式。

## 6. Current System and Evidence

| Observation | Source | Verification status | Design consequence |
| :--- | :--- | :--- | :--- |
| MCP 工具发现和 `robo_devices(list)` 正常 | 最小真实 LLM 会话 | Runtime-verified | 不修改模型工具注册 |
| headless `robo_devices(connect)` 成功 | 最小真实 LLM 会话 | Runtime-verified | 保留普通 Python MCP 进程 |
| visible connect 在 GLFW 创建窗口时崩溃 | 独立 MCP client 复现 | Runtime-verified | GUI 必须离开 MCP 进程 |
| 仿真状态可导出为 MJB 并由另一份 `MjData` 渲染 | MuJoCo Python 接口 | Runtime-verified | Viewer 可以拥有只读模型副本 |
| MCP 生命周期由 HardwareRuntime 管理 | `vibe/core/hardware/mcp_adapter.py` | Source-confirmed | Viewer 生命周期挂在适配器连接上 |

当前路径：

```text
LLM -> Robo tool -> HardwareRuntime -> MCP stdio -> Robosuite + in-process window
                                                   ^ macOS 主线程崩溃
```

## 7. Proposed Design

目标路径：

```text
LLM -> Robo tool -> HardwareRuntime -> MCP stdio -> Robosuite
                                                   |
                                                   | MJB model + latest state
                                                   v
                                             mmap state buffer
                                                   |
                                                   v
                                       native MuJoCo Viewer mirror
```

### 7.1 Ownership

`RobosuiteSimulatorAdapter` 继续拥有环境、动作、观测、验证和录像。它在 `_render()`
把最新 `qpos` 和 mocap 状态发布给一个私有 Viewer 模块。Viewer 第一次启动时还接收
由 MCP 导出的编译后 MJB 模型，并创建自己的 `MjData`，但绝不回写 MCP。

Viewer 模块提供小接口：

```python
viewer.publish(model, data)
viewer.close()
viewer.is_running
```

这个模块隐藏临时文件、MJB 导出、mmap 协议、子进程命令、进程存活检查和回收。
适配器不理解 IPC 格式，Viewer 也不导入 Robosuite 或硬件运行时。

### 7.2 State Protocol

状态缓冲区使用固定大小 mmap 文件。头部包含 magic、协议版本、`nq`、`nmocap`、
payload 大小和序列号；payload 包含 `qpos`、`mocap_pos` 和 `mocap_quat`。写入使用
奇偶序列号：奇数表示正在写，偶数表示稳定。Reader 仅在前后序列号相同且为偶数时
更新镜像，从而避免读取半个状态。

模型决定缓冲区尺寸；模型实例或尺寸变化时重建 Viewer。Viewer 使用 MuJoCo free
camera，窗口可调整大小，鼠标原生支持 orbit、pan、zoom 和选择新的观察焦点。

### 7.3 Lifecycle

1. `connect` 创建 Robosuite 环境并得到第一份状态。
2. 第一次 `publish` 导出 MJB、创建 state buffer、写入状态，再启动 Viewer。
3. 动作、home、stop 产生的新状态覆盖最新缓冲区。
4. 用户关闭 Viewer 时，仿真继续；当前连接不自动重新弹窗。
5. `disconnect`、Adapter `aclose` 或 MCP 正常退出时终止 Viewer 并删除 MJB 与临时目录。
6. Viewer 监视父 PID；MCP 异常退出后 Viewer 自行退出。

### 7.4 Failure Semantics

- Viewer 启动失败：记录到 MCP stderr，控制路径继续，`state.viewer=false`。
- Viewer 运行中退出：停止发布到该进程，控制路径继续。
- 状态维度错误：拒绝该状态，不修改上一稳定状态。
- MCP 异常退出：Viewer 通过父 PID 检测退出。
- headless：不创建 Viewer、缓冲区或 GUI 子进程。

## Alternatives

### Keep `mjpython` In-Process

Rejected. Runtime reproduction proves the offscreen GLFW context is still created on the Python worker
thread and crashes before OpenCV preview initialization.

### RGB Frame Polling

Rejected as the primary transport. RGB frames 不能提供真正的三维相机 orbit，只能做二维
裁切和缩放；持续传输高分辨率帧的开销也远高于十几个 `float64` 状态值。

### Socket Video Service

Deferred. It would support remote viewers but adds connection, authentication, framing and retry policy
that the local-only requirement does not need.

## Implementation Plan

1. Add a private Viewer module owning the MJB/state mmap protocol and GUI subprocess.
2. Replace in-process preview calls in the adapter with the Viewer interface.
3. Remove the macOS `mjpython` restart from the MCP entrypoint.
4. Add protocol, lifecycle, adapter integration and stdio MCP regression tests.
5. Update the runbook, README and builtin Robo skill.
6. Restore the user configuration to visible mode and run an end-to-end macOS probe.

## Validation Plan

- [ ] State buffer rejects malformed states and returns the stable latest state.
- [ ] Viewer subprocess starts once, receives updates and is reclaimed.
- [ ] Closing/failing Viewer does not fail adapter actions.
- [ ] headless stdio MCP regression remains green.
- [ ] visible stdio MCP connects without `mjpython` or `NSWindow` crash.
- [ ] a real Robo model turn can list/connect while the independent Viewer is alive.
- [ ] Ruff, Pyright and the full test suite pass.

<details>
<summary>Validation commands</summary>

```bash
uv run pytest tests/hardware_sim/test_viewer.py tests/hardware_sim/test_adapter.py tests/hardware_sim/test_mcp_server.py
uv run ruff check --fix .
uv run ruff format .
uv run pyright
uv run pytest
```

Visible macOS acceptance uses a real MCP client to call `robo_connect`, asserts a connected snapshot,
checks that the Viewer child is alive, then calls `robo_disconnect` and confirms cleanup.

</details>

## Rollout

No persisted schema migration is required. Existing `ROBO_SIM_HEADLESS=1` installations remain headless.
Removing the workaround enables the independent Viewer. If the Viewer fails, hardware control remains
available and users can re-enable headless without changing tools or session data.
