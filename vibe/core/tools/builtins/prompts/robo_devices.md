发现并管理 Robo 设备。先用 `list` 或 `describe` 读取能力，再 `connect` 和 `acquire`；任何运动前必须用当前租约显式 `arm`，结束后用 `disarm` 或 `robo_stop`。已武装设备不能直接释放租约。设备能力来自版本化 Manifest，不要假设不同机械臂拥有相同关节、坐标系或动作。
