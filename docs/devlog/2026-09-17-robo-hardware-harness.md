# 2026-09-17：Robo 硬件 Harness 与 CaP-X 接入

## 完成内容

- 新增会话级 `HardwareRuntime`，统一设备发现、连接、租约、显式武装/解除武装、动作、短序列、观察、停止和关闭生命周期。
- 新增确定性机械臂模拟器 `sim-arm-1`，支持回零、关节移动、夹爪、抓放和语义化叠衣动作。
- 新增 `DeviceCapabilityManifest` 与 stdio MCP Adapter；厂商 Adapter 的原始运动 Tool 必须从模型可见列表隐藏。
- 新增五个 Robo 类型化工具，并把同一个 Runtime 注入 Agent 工具调用上下文。
- 新增按 `run_id` 保存的硬件 JSONL Trace，以及 Git commit、branch、dirty files 工作区观察。
- 新增 `robo-capx-bridge`，实现 CaP-X 使用的 `/health` 与 `/chat/completions`，支持多模态 data URL 和多轮消息。
- 新增 `robo-capx-smoke`，使用白名单 AST 模拟 CaP-X 的 stdout/stderr 自修复回路。
- 并发动作在设备级原子仲裁，忙碌设备会确定性拒绝第二条动作；`run_id` 在任何设备副作用前校验，不能逃逸 Trace 目录。
- 模拟器收到停止 ACK 后会报告 `stopped`，不再错误显示为 `connected`。
- 动作必须同时满足连接、有效租约和显式武装；停止、关闭或租约失效后不会保留武装权限。
- 修复 AgentLoop 延迟初始化时丢失 MCP 硬件 Adapter 的问题。
- `FAILED` 或 `UNKNOWN` 回执会立即终止序列并请求停止；Runtime 关闭时也会停止并取消在途动作。
- 修复“停止与武装并发”竞态：在途武装收束后会再次确认停止。
- 模型可见 Trace 限制为 200 条事件和每条 16 KiB；CaP-X Trace 只记录输出长度，不保存原始模型内容。
- Trace 落盘失败只记录告警，不再阻断停止等安全关键操作。
- 对连接、解除武装、观察和停止回执校验设备 ID，并对动作回执同时校验设备 ID 与命令 ID；回执不匹配时失效本地设备状态并请求停止。
- 并发武装收束后的二次停止会同时校验设备 ID 和 ACK；错配或 NACK 时使设备失效，必须重新连接后才能继续控制。
- 将硬件 Adapter 与 MCP 调用协议拆入独立的 `_adapter_port.py`、`_mcp_port.py` 模块，保持运行时和传输实现解耦。

## 安全边界

- MCP 只是高层 Adapter 协议，运动不能绕过 Runtime。
- Tool cancellation 会另外请求 Adapter stop；只有 stop ACK 才能报告已停止。
- 本地模拟器不是实体设备安全验证，不能替代硬件急停、看门狗、限位或安全 PLC。
- CaP-X 生成的任意 Python 不在 Robo 主进程内执行。

## 本机验证

- HardwareRuntime、Robo tools、MCP Adapter、CaP-X HTTP bridge 和两轮修复测试均在 Mac 上通过。
- 聚焦硬件/CaP-X 测试 29 项通过；核心 Tools/AgentLoop 测试 123 项通过；传统 Tools/AgentLoop 回归 1,464 项通过、6 项跳过；CLI programmatic 测试 37 项通过。
- Ruff 全仓检查、Pyright（0 errors）、3,320 项 import contract、3,529 个 Pydantic model rebuild 和启动导入预算检查均通过。
- `robo-capx-bridge --help` 与 `robo-capx-smoke --help` 可执行。
- CaP-X 官方源码中的 `query_model()` 已实际调用 Robo Bridge 并正确解析返回代码。
- PyPI `robosuite==1.5.2` 搭配默认 MuJoCo 会触发 joint 类型断言；固定 `mujoco==3.3.7` 后，原生 `Lift/Panda` 的 `reset + step` 已通过。
- 使用上述兼容版本时，CaP-X 自己的 `FrankaRobosuiteCubeLiftLowLevel` 已完成初始化和 `reset`；运动调用因 PyPI 版缺少其定制 fork 的 `skip_render_images` 参数而停止。
- CaP-X 固定 Robosuite fork 的 Git 连接超时；不完整的 tarball 已验证损坏并清理。完整官方 Benchmark 仍需 Linux x86_64/NVIDIA。
- 全套 9,267 项测试曾启动；并行 E2E/ACP subprocess 在约 5% 时出现级联超时，因此中止。最早的 fresh-wheel CLI 失败已在未含本次实现的 `48bd17d` 临时 worktree 中独立复现，属于基线/本机 TUI 输入问题，不归因于本次硬件改动。
