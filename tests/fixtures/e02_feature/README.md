# Order discount

在 `src/order.py` 中实现：

`calculate_discount(subtotal: float) -> float`

函数返回折扣金额，而不是折后总价。

- subtotal 小于 0：抛出 `ValueError("subtotal must be non-negative")`
- subtotal 小于 100：折扣为 0
- subtotal 大于等于 100 且小于 500：折扣为 subtotal 的 5%
- subtotal 大于等于 500：折扣为 subtotal 的 10%
- 返回值使用 `round(value, 2)` 保留两位小数

