"""Every policy number of the transform, in one place."""

from __future__ import annotations

import re

PROVIDER = "Tive"
DEVICE_TYPE = "Active"

# R8 / R10: anything outside this range cannot be a physical temperature.
TEMP_RANGE_C: tuple[float, float] = (-100.0, 100.0)

# Documentation and tests only: every observed fault value lies outside TEMP_RANGE_C.
OBSERVED_FAULT_VALUES: tuple[float, ...] = (327.67, -327.68, 32767.0, -32768.0)

# R5: 2015-01-01T00:00:00Z .. now + 24 h
MIN_TIMESTAMP_MS = 1420070400000
MAX_FUTURE_MS = 86_400_000

# R6
UTC_TOLERANCE_MS = 1000

# Accuracy category: <= 50 m High, <= 500 m Medium, otherwise Low.
ACCURACY_THRESHOLDS: tuple[tuple[int, str], ...] = ((50, "High"), (500, "Medium"))
ACCURACY_FALLBACK = "Low"

LOCATION_SOURCE_MAP: dict[str, str] = {"gps": "GPS", "wifi": "WiFi", "cell": "Cellular"}

IMEI_PATTERN = re.compile(r"[0-9]{15}")

# R11 secondary ranges (inclusive)
HUMIDITY: tuple[float, float] = (0.0, 100.0)
BATTERY: tuple[float, float] = (0.0, 100.0)
DBM: tuple[float, float] = (-150.0, -50.0)
LUX_MIN = 0.0
