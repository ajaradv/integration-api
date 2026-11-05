"""Numeric helpers. Rounding goes through Decimal so 2.675 -> 2.68."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(value: float | int | None, places: int) -> float | None:
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def to_int(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
