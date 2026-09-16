请求设备停止并返回设备 ACK。此工具始终可调用。只有 `acknowledged=true` 才能确认 Adapter 报告已停止；协议取消或没有 ACK 不能当作急停成功，实体设备仍需独立硬件急停。
