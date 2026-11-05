"""Stage 3: Tive -> PX mapping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.transform import constants as c
from app.transform.address import split_address
from app.transform.rounding import round_half_up, to_int
from app.transform.rules import Computed, Nullify
from app.transform.tive_fields import ParsedView


@dataclass(frozen=True)
class SensorExtras:
    probe_temperature: float | None
    probe_fault: bool
    account_id: int | None
    entity_name: str | None
    shipment_id: str | None


def _nulled(nullify: Sequence[Nullify], target: str) -> bool:
    return any(n.target == target for n in nullify)


def _value(view: ParsedView, path: str) -> Any:
    return view.get(path)


def _accelerometer(
    view: ParsedView, computed: Sequence[Computed]
) -> dict[str, float | None] | None:
    if view.container_state("Accelerometer") != "ok":
        return None
    magnitude = _value(view, "Accelerometer.G")
    for item in computed:
        if item.name == "magnitude":
            magnitude = item.value
    return {
        "x": round_half_up(_value(view, "Accelerometer.X"), 3),
        "y": round_half_up(_value(view, "Accelerometer.Y"), 3),
        "z": round_half_up(_value(view, "Accelerometer.Z"), 3),
        "magnitude": round_half_up(magnitude, 3),
    }


def to_px_sensor(
    view: ParsedView, nullify: Sequence[Nullify], computed: Sequence[Computed]
) -> dict[str, Any]:
    return {
        "device_id": _value(view, "DeviceName"),
        "device_imei": _value(view, "DeviceId"),
        "timestamp": _value(view, "EntryTimeEpoch"),
        "provider": c.PROVIDER,
        "type": c.DEVICE_TYPE,
        "temperature": round_half_up(_value(view, "Temperature.Celsius"), 2),
        "humidity": None
        if _nulled(nullify, "humidity")
        else round_half_up(_value(view, "Humidity.Percentage"), 1),
        "light_level": None
        if _nulled(nullify, "light_level")
        else round_half_up(_value(view, "Light.Lux"), 1),
        "accelerometer": _accelerometer(view, computed),
        "tilt": None,
        "box_open": None,
    }


def accuracy_category(meters: float | None) -> str | None:
    if meters is None:
        return None
    for limit, label in c.ACCURACY_THRESHOLDS:
        if meters <= limit:
            return label
    return c.ACCURACY_FALLBACK


def to_px_location(view: ParsedView, nullify: Sequence[Nullify]) -> dict[str, Any]:
    meters = (
        None if _nulled(nullify, "location_accuracy") else _value(view, "Location.Accuracy.Meters")
    )
    method = _value(view, "Location.LocationMethod")
    return {
        "device_id": _value(view, "DeviceName"),
        "device_imei": _value(view, "DeviceId"),
        "timestamp": _value(view, "EntryTimeEpoch"),
        "provider": c.PROVIDER,
        "type": c.DEVICE_TYPE,
        "latitude": _value(view, "Location.Latitude"),
        "longitude": _value(view, "Location.Longitude"),
        "altitude": None,
        "location_accuracy": to_int(meters),
        "location_accuracy_category": accuracy_category(meters),
        "location_source": c.LOCATION_SOURCE_MAP.get(method) if method is not None else None,
        "address": split_address(_value(view, "Location.FormattedAddress")),
        "battery_level": None
        if _nulled(nullify, "battery_level")
        else _value(view, "Battery.Percentage"),
        "cellular_dbm": None
        if _nulled(nullify, "cellular_dbm")
        else round_half_up(_value(view, "Cellular.Dbm"), 2),
        "cellular_network_type": None,
        "cellular_operator": None,
        "wifi_access_points": _value(view, "Location.WifiAccessPointUsedCount"),
    }


def extras(view: ParsedView, nullify: Sequence[Nullify]) -> SensorExtras:
    fault = any(n.target == "probe_temperature" and n.fault for n in nullify)
    probe = (
        None if _nulled(nullify, "probe_temperature") else _value(view, "ProbeTemperature.Celsius")
    )
    return SensorExtras(
        probe_temperature=round_half_up(probe, 2),
        probe_fault=fault,
        account_id=_value(view, "AccountId"),
        entity_name=_value(view, "EntityName"),
        shipment_id=_value(view, "ShipmentId"),
    )
