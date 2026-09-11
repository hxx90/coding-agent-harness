def validate_username(username: str | None) -> str:
    if username is None:
        raise ValueError("username is required")
    return username

