用一段受限 Python 代码控制 Robo 设备完成物理任务。这是唯一可改变设备物理状态的模型入口：不要寻找或调用单步动作工具。先用 `robo_devices describe` 读取 Manifest；代码中可直接调用 Manifest 动作名，也可使用 `act("动作名", 参数名=参数值)`，并使用 `observe()`、`verify("条件", 参数名=参数值)`、`stop()`、`print()`。策略应形成“观察—动作—再观察—验证”的闭环；只有结果中的 `policy.task_outcome="passed"` 才能宣称任务成功。

代码支持变量、JSON 值、下标、算术/比较/布尔表达式、`if`、`for`/`while`、`break`/`continue`，以及 `len/range/min/max/abs/round/int/float/str/bool`。禁止 import、属性访问、任意函数或类定义、文件/网络/进程访问及参数展开。整棵语法树会在动作前校验。

**正常模式默认不限制每策略的执行语句数、循环次数、动作次数或运行时长**。计算循环会主动让出执行权，用户可以随时暂停/取消。只有显式配置的评测模式才应用额外执行额度。源码/结构化数据的格式校验与资源保护仍保留。

动作或代码失败会停止当前策略并请求停止设备，返回错误码、源码 URI 和行列；正常任务会把错误交还给你继续分析，而不是结束整个任务。先观察最新状态，必要时重新武装，再生成修正后的策略；不要盲目重放已经完成的动作。保留动作后观察和独立 verify，控制到位不能替代任务成功。完整策略轨迹持续写入 result.trace_uri；返回的 steps/stdout/receipts 是最近记录，history_truncated=true 表示更早记录在文件中，不表示执行失败。

循环要根据实际观测判断进展和退出条件；未达到任务目标时不能宣称完成。

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
