用一段受限 Python 代码控制 Robo 设备完成物理任务。这是唯一可改变设备物理状态的模型入口：不要寻找或调用单步动作工具。先用 `robo_devices describe` 读取 Manifest；代码中可直接调用 Manifest 动作名，也可使用 `act("动作名", 参数名=参数值)`，并使用 `observe()`、`verify("条件", 参数名=参数值)`、`stop()`、`print()`。策略应形成“观察—动作—再观察—验证”的闭环；只有结果中的 `policy.task_outcome="passed"` 才能宣称任务成功。

代码支持变量、JSON 值、下标、算术/比较/布尔表达式、`if`、有界 `for`/`while`、`break`/`continue`，以及 `len/range/min/max/abs/round/int/float/str/bool`。禁止 import、属性访问、任意函数或类定义、文件/网络/进程访问及参数展开。整棵语法树会在动作前校验；源码、语句、循环、动作、输出和运行时间都有上限。运行错误会返回错误码、源码 URI 和行列，模型应根据诊断重写完整策略，不要盲目重放；错误、取消或超时会请求停止设备。

示例：

```python
before = observe()
fold_cloth(cloth="shirt")
after = observe()
report = verify("cloth_folded", cloth="shirt")
print(report["status"])
```
