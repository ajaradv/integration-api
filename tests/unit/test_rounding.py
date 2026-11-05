import pytest

from app.transform.rounding import round_half_up, to_int


@pytest.mark.parametrize(
    ("value", "places", "expected"),
    [
        (2.675, 2, 2.68),
        (-0.5625, 3, -0.563),
        (-0.4375, 3, -0.438),
        (0.6875, 3, 0.688),
        (38.70000076293945, 1, 38.7),
        (10.078125, 2, 10.08),
        (0, 1, 0.0),
        (-100, 2, -100.0),
        (0.05, 1, 0.1),
    ],
)
def test_round_half_up(value: float, places: int, expected: float) -> None:
    assert round_half_up(value, places) == expected


def test_round_none() -> None:
    assert round_half_up(None, 2) is None
    assert to_int(None) is None


@pytest.mark.parametrize(
    ("value", "expected"), [(23, 23), (22.5, 23), (500, 500), (0.4, 0), (-1.5, -2), (31.49, 31)]
)
def test_to_int(value: float, expected: int) -> None:
    assert to_int(value) == expected
    assert isinstance(to_int(value), int)
