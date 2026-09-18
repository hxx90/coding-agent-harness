按顺序执行一组 Robo 设备动作。任一步失败时 Runtime 会请求停止设备。只用于已确认的短序列；长任务应分阶段观察后继续，避免在未知物理状态下重放。返回的 `completed` 只表示控制命令执行结束，`task_outcome` 固定为 `not_verified`；完成任务前必须重新观察并调用 `robo_verify`。
