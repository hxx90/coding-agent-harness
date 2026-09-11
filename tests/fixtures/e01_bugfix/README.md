# User validation

`validate_username` 接收用户名并返回原值。

当用户名是 `None`、空字符串或只包含空白字符时，
必须抛出 `ValueError("username is required")`。

