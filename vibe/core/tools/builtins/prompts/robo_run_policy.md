用一段受限 Python 代码控制 Robo 设备完成物理任务。这是唯一可改变设备物理状态的模型入口：不要寻找或调用单步动作工具。先用 `robo_devices describe` 读取 Manifest；代码中可直接调用 Manifest 动作名，也可使用 `act("动作名", 参数名=参数值)`，并使用 `observe()`、`verify("条件", 参数名=参数值)`、`stop()`、`print()`。策略应形成“观察—动作—再观察—验证”的闭环；只有结果中的 `policy.task_outcome="passed"` 才能宣称任务成功。

代码支持变量、JSON 值、下标、算术/比较/布尔表达式、`if`、有界 `for`/`while`、`break`/`continue`，以及 `len/range/min/max/abs/round/int/float/str/bool`。禁止 import、属性访问、任意函数或类定义、文件/网络/进程访问及参数展开。整棵语法树会在动作前校验；源码、语句、循环、动作、输出和运行时间都有上限。运行错误会返回错误码、源码 URI 和行列，不要自动重放已执行动作；错误、取消或超时会请求停止设备。

默认执行预算是**每策略 200 条执行语句、50 次动作、累计 50 次循环迭代、120 秒**。这些是整段策略各阶段共用的额度，不是每个循环分别有 200 条；下一次策略调用才重新计数。赋值、表达式语句（含 observe/action/verify/print）、if、循环节点、break/continue/pass 每执行一次各计一条。循环节点进入时计一条，循环体中的语句每轮重新计数；条件求值、下标和算术子表达式不另计。一次 resting_geometry 调用内部的 NumPy 点云计算不逐点计数；move_cartesian 的 steps 是动作内部控制步数，也不逐步扣解释器语句。

生成前核算「初始化 + 各循环每轮语句数 × 最大轮数 + 抓取/抬升/观察/验证」的总开销，为最后验证保留余量。几十行源码不代表只执行几十条语句。用紧凑的反馈循环和内联算术，避免在每轮重复兼容分支、拆分大量临时变量或重算未改变的几何。循环要有退出条件；未达到条件时不能直接宣称完成。不要通过省略动作后观察或 verify 来压缩预算，也不要用重新调用策略来绕开额度。

`observe()` 返回完整 DeviceSnapshot，始终通过 `snapshot["state"]` 访问设备状态。RGB-D 设备的点云是 `snapshot["state"]["perception"]["points"]`，末端位置是 `snapshot["state"]["end_effector_position"]`；**perception 不在 snapshot 顶层**。无需在循环内反复检查是否有 state 字段。`verify()` 返回 VerificationReport，其 `status` 在报告顶层。控制量和单位按 Manifest；0.05 m 是 OSC 目标增量缩放，不保证每步的实际位移，必须用更新后的本体观测判断进展。

若 Manifest 提供 `move_to`，可提交固定世界坐标米制 `target_xyz`，由设备内部逐控制周期反馈；参数及边界以 Manifest 为准。返回回执的 `result.motion` 含 `reached/reason/error_xyz/error_max_m/cycles/settled_cycles/elapsed_seconds`。未到位会返回失败并中止本策略，后续下降、闭爪等语句不会执行；取消仍按取消处理，可观察 `state.last_target_move` 查结束原因。到位只证明控制目标达到，不能替代 `verify("lift_object")`。原 `move_cartesian` 仍会重复施加增量 `steps` 次，不是等待一个固定目标收敛。`state.control_cycles_remaining` 是所有运动共用的会话余量（若配置），重复调用不会补充；安全停止额度独立。

动作返回完整 `CommandReceipt` 字典：`{"status": "completed", "result": {"motion": {...}}}`（此处为 move_to 的结构示意）。`move_to(...)` 与 `act("move_to", ...)` 返回相同结构，均不会自动拆出 result。读取到位状态用 `receipt["result"]["motion"]["reached"]`；`receipt["motion"]` 不存在。`verify()` 返回的是另一种报告，其顶层 `status` 不能套用为动作的到位字段。

回执读取示例（仅保持当前观测位置，展示返回值访问，不是抓取策略）：

```python
before = observe()
receipt = move_to(target_xyz=before["state"]["end_effector_position"])
motion = receipt["result"]["motion"]
print(motion["reached"], motion["reason"], motion["error_max_m"], motion["cycles"])
```

若设备 Manifest 明确声明 `metadata.geometry_library="h1-v8-v9"`，可调用只读
`resting_geometry(points, ...)`，参数和说明见同一 Manifest 的 `geometry_notes`。
它最多接受 128 个米制 XYZ 点，输出 `ok` 只表示几何估计有效，不表示任务成功。
库关闭时此函数不在 AST 白名单。使用当前 `state.perception` 的相机点云，
动作仍走原有 Manifest 接口。比较符支持 `== != < <= > >= in not in`，不支持 `is`。
