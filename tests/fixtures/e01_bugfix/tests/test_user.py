import pytest

from src.user import validate_username


@pytest.mark.parametrize("username", [None, "", "   "])
def test_rejects_missing_username(username):
    with pytest.raises(ValueError, match="username is required"):
        validate_username(username)


def test_returns_valid_username():
    assert validate_username("alice") == "alice"

