import pytest

from src.order import calculate_discount


@pytest.mark.parametrize(
    ("subtotal", "expected"),
    [
        (0, 0.0),
        (99.99, 0.0),
        (100, 5.0),
        (499.99, 25.0),
        (500, 50.0),
        (1234.56, 123.46),
    ],
)
def test_calculate_discount(subtotal, expected):
    assert calculate_discount(subtotal) == expected


def test_rejects_negative_subtotal():
    with pytest.raises(ValueError, match="subtotal must be non-negative"):
        calculate_discount(-0.01)

