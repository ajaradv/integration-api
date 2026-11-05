"""Stage 2: business rules R0-R12. Every rule runs; none short-circuits another."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from app.transform import constants as c
from app.transform.issues import Issue, IssueCode, Severity
from app.transform.tive_fields import ParsedView


@dataclass(frozen=True)
class Nullify:
    """Set a PX field to null. `fault` marks a probe fault."""

    target: str
    fault: bool = False


@dataclass(frozen=True)
class Computed:
    name: str
    value: float


RuleOutput = Issue | Nullify | Computed
Rule = Callable[[ParsedView, int], list[RuleOutput]]


def _blocking(code: IssueCode, field: str, message: str, original: object = None) -> Issue:
    return Issue(code, field, Severity.BLOCKING, message, original=original)


def _warning(code: IssueCode, field: str, message: str, original: object = None) -> Issue:
    return Issue(code, field, Severity.WARNING, message, original=original)


def r0_required_structure(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    out: list[RuleOutput] = []

    def missing(field: str, why: str) -> None:
        out.append(_blocking(IssueCode.MISSING_FIELD, field, f"{field} is {why}"))

    if view.container_state("Temperature") != "ok":
        missing("Temperature", "required and must be an object")
        missing("Temperature.Celsius", "required")
    elif not view.present("Temperature.Celsius"):
        missing("Temperature.Celsius", "required (the key must be present, null is allowed)")
    if view.container_state("Location") != "ok":
        missing("Location", "required and must be an object")
        missing("Location.Latitude", "required")
        missing("Location.Longitude", "required")
    else:
        for path in ("Location.Latitude", "Location.Longitude"):
            if view.is_valid(path) and not view.has_value(path):
                missing(path, "required and must not be null")
    if view.is_valid("EntryTimeEpoch") and not view.has_value("EntryTimeEpoch"):
        missing("EntryTimeEpoch", "required and must not be null")
    return out


def r1_device_imei(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if not view.is_valid("DeviceId"):
        return []
    value = view.get("DeviceId")
    if value is None:
        return [_blocking(IssueCode.DEVICE_IMEI_MISSING, "DeviceId", "DeviceId is missing")]
    if not c.IMEI_PATTERN.fullmatch(value):
        return [
            _blocking(
                IssueCode.DEVICE_IMEI_MALFORMED,
                "DeviceId",
                "DeviceId must be a 15-digit IMEI",
                value,
            )
        ]
    return []


def r2_device_name(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if not view.is_valid("DeviceName"):
        return []
    value = view.get("DeviceName")
    if value is None or not value.strip():
        return [
            _blocking(
                IssueCode.DEVICE_ID_MISSING, "DeviceName", "DeviceName is missing or empty", value
            )
        ]
    return []


def r3_entity_name(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if not (view.has_value("EntityName") and view.has_value("DeviceName")):
        return []
    if view.get("EntityName") != view.get("DeviceName"):
        return [
            _warning(
                IssueCode.ENTITY_NAME_DIFFERS,
                "EntityName",
                "EntityName differs from DeviceName; stored as metadata only",
                view.get("EntityName"),
            )
        ]
    return []


def r4_shipment_device(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if not (view.has_value("Shipment.DeviceId") and view.has_value("DeviceId")):
        return []
    if view.get("Shipment.DeviceId") != view.get("DeviceId"):
        return [
            _warning(
                IssueCode.SHIPMENT_DEVICE_MISMATCH,
                "Shipment.DeviceId",
                "Shipment.DeviceId differs from DeviceId",
                view.get("Shipment.DeviceId"),
            )
        ]
    return []


def r5_timestamp_plausible(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if not view.has_value("EntryTimeEpoch"):
        return []
    epoch = view.get("EntryTimeEpoch")
    if epoch < c.MIN_TIMESTAMP_MS or epoch > now_ms + c.MAX_FUTURE_MS:
        return [
            _blocking(
                IssueCode.TIMESTAMP_IMPLAUSIBLE,
                "EntryTimeEpoch",
                "EntryTimeEpoch must be epoch milliseconds between 2015-01-01 and now + 24 h; "
                "it is never converted automatically",
                epoch,
            )
        ]
    return []


def r6_timestamp_agrees(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if not (view.has_value("EntryTimeEpoch") and view.has_value("EntryTimeUtc")):
        return []
    epoch = view.get("EntryTimeEpoch")
    utc_ms = view.get("EntryTimeUtc")
    if abs(epoch - utc_ms) > c.UTC_TOLERANCE_MS:
        return [
            _warning(
                IssueCode.TIMESTAMP_MISMATCH,
                "EntryTimeUtc",
                f"EntryTimeUtc differs from EntryTimeEpoch by {abs(epoch - utc_ms)} ms",
                utc_ms,
            )
        ]
    return []


def r7_coordinates(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    out: list[RuleOutput] = []
    lat = view.get("Location.Latitude")
    lon = view.get("Location.Longitude")
    if view.has_value("Location.Latitude") and not -90 <= lat <= 90:
        out.append(
            _blocking(
                IssueCode.LATITUDE_OUT_OF_RANGE,
                "Location.Latitude",
                "latitude must be within [-90, 90]",
                lat,
            )
        )
    if view.has_value("Location.Longitude") and not -180 <= lon <= 180:
        out.append(
            _blocking(
                IssueCode.LONGITUDE_OUT_OF_RANGE,
                "Location.Longitude",
                "longitude must be within [-180, 180]",
                lon,
            )
        )
    return out


def _outside_temp(value: float) -> bool:
    return not c.TEMP_RANGE_C[0] <= value <= c.TEMP_RANGE_C[1]


def r8_temperature_fault(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if view.has_value("Temperature.Celsius") and _outside_temp(view.get("Temperature.Celsius")):
        return [
            _blocking(
                IssueCode.TEMPERATURE_FAULT,
                "Temperature.Celsius",
                "temperature outside [-100, 100] C looks like a sensor fault value",
                view.get("Temperature.Celsius"),
            )
        ]
    return []


def r9_temperature_null(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if (
        view.container_state("Temperature") == "ok"
        and view.present("Temperature.Celsius")
        and view.is_valid("Temperature.Celsius")
        and view.get("Temperature.Celsius") is None
    ):
        return [
            _warning(
                IssueCode.TEMPERATURE_NULL,
                "Temperature.Celsius",
                "Temperature.Celsius is null; PX temperature will be null",
            )
        ]
    return []


def r10_probe_fault(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if view.has_value("ProbeTemperature.Celsius") and _outside_temp(
        view.get("ProbeTemperature.Celsius")
    ):
        return [
            _warning(
                IssueCode.PROBE_FAULT,
                "ProbeTemperature.Celsius",
                "probe temperature outside [-100, 100] C looks like a sensor fault value; "
                "stored as null with probe_fault = true",
                view.get("ProbeTemperature.Celsius"),
            ),
            Nullify("probe_temperature", fault=True),
        ]
    return []


def r11_secondary_ranges(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    out: list[RuleOutput] = []
    checks: list[tuple[str, IssueCode, str, Callable[[float], bool], str]] = [
        (
            "Humidity.Percentage",
            IssueCode.HUMIDITY_OUT_OF_RANGE,
            "humidity",
            lambda v: not c.HUMIDITY[0] <= v <= c.HUMIDITY[1],
            "humidity must be within [0, 100]",
        ),
        (
            "Light.Lux",
            IssueCode.LUX_OUT_OF_RANGE,
            "light_level",
            lambda v: v < c.LUX_MIN,
            "lux must not be negative",
        ),
        (
            "Battery.Percentage",
            IssueCode.BATTERY_OUT_OF_RANGE,
            "battery_level",
            lambda v: not c.BATTERY[0] <= v <= c.BATTERY[1],
            "battery must be within [0, 100]",
        ),
        (
            "Cellular.Dbm",
            IssueCode.DBM_OUT_OF_RANGE,
            "cellular_dbm",
            lambda v: not c.DBM[0] <= v <= c.DBM[1],
            "dBm must be within [-150, -50]",
        ),
        (
            "Location.Accuracy.Meters",
            IssueCode.ACCURACY_OUT_OF_RANGE,
            "location_accuracy",
            lambda v: v < 0,
            "accuracy meters must not be negative",
        ),
    ]
    for path, code, target, bad, message in checks:
        if view.has_value(path) and bad(view.get(path)):
            out.append(_warning(code, path, f"{message}; PX value set to null", view.get(path)))
            out.append(Nullify(target))
    return out


def r12_magnitude(view: ParsedView, now_ms: int) -> list[RuleOutput]:
    if view.has_value("Accelerometer.G"):
        return []
    if not view.is_valid("Accelerometer.G") and view.present("Accelerometer.G"):
        return []
    axes = [view.get(f"Accelerometer.{a}") for a in "XYZ"]
    if all(view.has_value(f"Accelerometer.{a}") for a in "XYZ"):
        magnitude = math.sqrt(sum(v * v for v in axes))
        return [
            Issue(
                IssueCode.MAGNITUDE_COMPUTED,
                "Accelerometer.G",
                Severity.CORRECTION,
                "magnitude computed from X, Y and Z",
                original=None,
                corrected=magnitude,
            ),
            Computed("magnitude", magnitude),
        ]
    return []


RULES: tuple[Rule, ...] = (
    r0_required_structure,
    r1_device_imei,
    r2_device_name,
    r3_entity_name,
    r4_shipment_device,
    r5_timestamp_plausible,
    r6_timestamp_agrees,
    r7_coordinates,
    r8_temperature_fault,
    r9_temperature_null,
    r10_probe_fault,
    r11_secondary_ranges,
    r12_magnitude,
)


def run_rules(view: ParsedView, now_ms: int) -> tuple[list[Issue], list[Nullify], list[Computed]]:
    issues: list[Issue] = []
    nullify: list[Nullify] = []
    computed: list[Computed] = []
    for rule in RULES:
        for item in rule(view, now_ms):
            if isinstance(item, Issue):
                issues.append(item)
            elif isinstance(item, Nullify):
                nullify.append(item)
            else:
                computed.append(item)
    return issues, nullify, computed
