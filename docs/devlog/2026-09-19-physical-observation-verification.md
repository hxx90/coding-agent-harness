# 物理观察与任务验证闭环

日期：2026-09-19

## 问题

一次 MuJoCo 会话中，Robo 成功执行了末端移动和夹爪闭合，并据此宣称抓取任务完成；但 Trace 显示方块位移接近零，夹爪与方块仍相距约 0.23 米。根因是系统把控制命令的 `completed` 回执误当成了任务成功，而且模型只能读取数值快照，无法看到模拟器画面。

## 修复

- `CommandReceipt` 继续只表达控制命令是否执行结束；Robo 工具额外返回 `task_outcome = not_verified`。
- 新增设备声明的验证能力以及 `robo_verify`，结果只有 `passed`、`failed`、`inconclusive` 三种。
- MuJoCo `lift_object` 验证同时检查方块是否被夹持，以及观测到的方块高度是否超过桌面 0.04 米；不会调用或改名暴露仿真 reward / success oracle。
- `robo_observe` 和 `robo_verify` 采集新的 `agentview` RGB 图像，将证据保存到当前 session 的 `hardware-runs/evidence/`。
- 证据文件必须位于 Runtime 管理的目录内、类型受支持且小于 10 MiB，避免设备 Adapter 诱导模型读取任意本地文件。
- 支持视觉的模型会在下一次推理中收到证据图像；不支持视觉的模型仍可使用结构化位置、距离、抓取和任务状态。
- 验证报告携带证据 ID，Trace 记录验证结论，便于复盘“模型为什么认为任务完成”。
- 当前 Turn 执行了设备声明可验证的物理动作后，AgentLoop 会通过类型化 UI 事件提示并阻止直接结束，要求调用与该动作及参数匹配的 `robo_verify`；报告时间和快照序号必须晚于动作。验证失败或证据不足时可以停止并如实报告，但不能宣称成功。

## 新行为

完成动作后必须重新观察并调用 `robo_verify`。只有验证结果为 `passed` 时，Robo 才能宣称物理任务完成；`failed` 应继续修正，`inconclusive` 应补充视角或请求人工确认。
