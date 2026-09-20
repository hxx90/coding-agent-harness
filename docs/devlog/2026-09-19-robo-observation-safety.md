# 2026-09-19：Robo 视觉观测、硬件 UI 与安全恢复

## 完成内容

- MuJoCo 改为单一离屏相机上下文，由 Robo 使用 OpenCV 显示预览，避免 macOS
  上 onscreen/offscreen 双 GL context 冲突。
- 修复 `agentview_image` 上下方向，RGB 证据、预览和录像使用同一正向画面。
- 新增原生 MP4 录制。Robo 管理的 MCP Adapter 默认写入
  `hardware-runs/recordings/<server>/`，`robo_observe` 通过
  `snapshot.state.recording_uri` 返回录像 URI。
- 六个 `robo_*` 工具新增统一 `hardware` Effect，Textual 卡片可直接显示设备、
  物理状态、故障、证据 URI、最近 Trace、命令回执、任务验证和工作区状态。
- 新增 App Server `hardware/stop` 与 `Ctrl+Shift+X` 全局停止。停止请求优先于
  Agent turn 中断，并覆盖已连接、执行中及正在连接的设备。
- `stop -> release` 支持幂等重试；已武装或仍有活动租约的设备不能直接重连；
  停止并释放后，重连会断开旧连接，且必须获取新租约、重新武装。
- 全局停止超时后会保留旧连接代际的占用，直到原连接任务退出，避免重试连接覆盖
  旧任务并被其错误激活；Runtime 关闭时会停止、取消并等待正在连接的设备。
- 修复 `robo_execute` 因缺少 `command_id` 投影字段而退化成通用工具卡片的问题，
  并补齐设备发现列表和 `not_verified` 任务门禁展示。

## 安全边界

- `Ctrl+Shift+X` 是 Harness 内的软件停止，只在设备 Adapter 返回 ACK 后报告
  成功，不能替代实体设备上的物理急停、安全 PLC、限位和独立看门狗。
- Unified backend 当前不持有 Python `HardwareRuntime`，会明确返回
  `NOT_IMPLEMENTED`；默认 Legacy backend 已接通完整链路。
- 命令 `completed` 仍不代表物理任务成功。模型必须观察新快照并调用
  `robo_verify`，只有 `passed` 才能宣称任务完成。

## 本机验证

- Runtime、App Server、硬件 Effect、Textual 卡片和 MuJoCo stdio 聚焦回归
  171 项通过；另有 36 项 App Server 架构边界测试通过。Pyright 为 0 errors，
  Ruff check/format 和变更文件 typos 检查通过。
- 实际 MuJoCo stdio 测试生成了非空 MP4 文件。
- 全量 9,306 项 pytest 再次在约 5% 出现既有的 subprocess/ACP 超时级联，已
  中止；首个 ACP 启动失败单独复跑仍为 10 秒启动超时，不涉及硬件代码路径。
- pre-commit 两次在安装固定版 Pyright 的 Node 环境时被远端重置连接；同一
  工作树已用本地 Pyright、Ruff 和 typos 分别完成检查。
