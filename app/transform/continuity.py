"""R13: location continuity against the device's neighbouring stored readings. Performs no I/O.

The other rules look at one payload. R13 compares the new PX location with the stored reading
just before it and the one just after it (same device, strictly earlier / later timestamp) and
asks whether the device could physically have travelled between the two points:

    d      = haversine distance between the two fixes                     (m)
    d_eff  = max(0, d - (u_1 + u_2))     u = location accuracy, or the default (m)
    dt     = |t_2 - t_1| / 1000                                            (s)
    flag if  d_eff > v_max * dt + d_floor
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.transform import constants as c
from app.transform.issues import Issue, IssueCode, Severity


@dataclass(frozen=True)
class Neighbour:
    """A stored reading of the same device, used as the R13 baseline."""

    ingestion_id: UUID
    timestamp_ms: int
    latitude: float
    longitude: float
    accuracy_m: float | None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points given in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * c.EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _accuracy(value: float | None) -> float:
    if value is None or value < 0:
        return c.LOCATION_JUMP_DEFAULT_ACCURACY_M
    return float(value)


def checkable(px_location: dict[str, Any] | None) -> bool:
    """True when the PX location has the device, timestamp and in-range coordinates R13 needs."""
    if not px_location:
        return False
    imei = px_location.get("device_imei")
    ts = px_location.get("timestamp")
    lat = px_location.get("latitude")
    lon = px_location.get("longitude")
    return (
        isinstance(imei, str)
        and c.IMEI_PATTERN.fullmatch(imei) is not None
        and isinstance(ts, int)
        and not isinstance(ts, bool)
        and isinstance(lat, int | float)
        and isinstance(lon, int | float)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def check_location_jump(
    px_location: dict[str, Any],
    previous: Neighbour | None,
    following: Neighbour | None,
    acknowledged: bool = False,
) -> list[Issue]:
    """One issue per neighbour the new fix could not have been reached from (or reach).

    `acknowledged` (operator promotion) keeps each issue as a warning instead of blocking.
    """
    if not checkable(px_location):
        return []
    ts: int = px_location["timestamp"]
    lat: float = px_location["latitude"]
    lon: float = px_location["longitude"]
    accuracy = _accuracy(px_location.get("location_accuracy"))
    out: list[Issue] = []
    for label, n in (("previous", previous), ("next", following)):
        if n is None or n.timestamp_ms == ts:
            continue
        distance = haversine_m(n.latitude, n.longitude, lat, lon)
        effective = max(0.0, distance - (accuracy + _accuracy(n.accuracy_m)))
        elapsed_s = abs(ts - n.timestamp_ms) / 1000
        allowed = c.LOCATION_JUMP_MAX_SPEED_MPS * elapsed_s + c.LOCATION_JUMP_FLOOR_M
        if effective <= allowed:
            continue
        speed_kmh = effective / elapsed_s * 3.6
        detail = {
            "neighbour": label,
            "ingestion_id": str(n.ingestion_id),
            "timestamp_ms": n.timestamp_ms,
            "latitude": n.latitude,
            "longitude": n.longitude,
            "distance_m": round(distance),
            "effective_distance_m": round(effective),
            "allowed_distance_m": round(allowed),
            "elapsed_s": elapsed_s,
            "implied_speed_kmh": round(speed_kmh, 1),
            "max_speed_kmh": round(c.LOCATION_JUMP_MAX_SPEED_MPS * 3.6, 1),
        }
        message = (
            f"location is {effective / 1000:.1f} km beyond GPS error from the {label} reading "
            f"{elapsed_s:.0f} s away ({speed_kmh:.0f} km/h implied, max "
            f"{detail['max_speed_kmh']:.0f} km/h)"
        )
        out.append(
            Issue(
                IssueCode.LOCATION_JUMP,
                "Location",
                Severity.WARNING if acknowledged else Severity.BLOCKING,
                f"{message}; acknowledged by operator" if acknowledged else message,
                original=detail,
                source="operator" if acknowledged else "pipeline",
            )
        )
    return out
