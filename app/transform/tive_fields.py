"""Stage 1: parse every Tive field on its own.

The payload is never parsed as one model. A field that fails becomes None, is
marked invalid and produces an issue; the other fields are still parsed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.transform.constants import LOCATION_SOURCE_MAP
from app.transform.issues import Issue, IssueCode, Severity

Group = Literal["strict", "primary", "secondary"]
Kind = Literal["str", "int", "num", "datetime", "method"]

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

# Containers whose absence is reported by rule R0, not by the parser.
_R0_CONTAINERS = frozenset({"Temperature", "Location"})


@dataclass(frozen=True)
class FieldSpec:
    group: Group
    kind: Kind


FIELDS: dict[str, FieldSpec] = {
    # strict: identity, time, coordinates
    "EntityName": FieldSpec("strict", "str"),
    "DeviceName": FieldSpec("strict", "str"),
    "DeviceId": FieldSpec("strict", "str"),
    "EntryTimeEpoch": FieldSpec("strict", "int"),
    "EntryTimeUtc": FieldSpec("strict", "datetime"),
    "Location.Latitude": FieldSpec("strict", "num"),
    "Location.Longitude": FieldSpec("strict", "num"),
    # primary measurement
    "Temperature.Celsius": FieldSpec("primary", "num"),
    # secondary measurements
    "Temperature.Fahrenheit": FieldSpec("secondary", "num"),
    "ProbeTemperature.Celsius": FieldSpec("secondary", "num"),
    "ProbeTemperature.Fahrenheit": FieldSpec("secondary", "num"),
    "Humidity.Percentage": FieldSpec("secondary", "num"),
    "Light.Lux": FieldSpec("secondary", "num"),
    "Accelerometer.G": FieldSpec("secondary", "num"),
    "Accelerometer.X": FieldSpec("secondary", "num"),
    "Accelerometer.Y": FieldSpec("secondary", "num"),
    "Accelerometer.Z": FieldSpec("secondary", "num"),
    "Battery.Percentage": FieldSpec("secondary", "int"),
    "Cellular.Dbm": FieldSpec("secondary", "num"),
    "Location.Accuracy.Meters": FieldSpec("secondary", "num"),
    "Location.WifiAccessPointUsedCount": FieldSpec("secondary", "int"),
    "Location.FormattedAddress": FieldSpec("secondary", "str"),
    "Location.LocationMethod": FieldSpec("secondary", "method"),
    "AccountId": FieldSpec("secondary", "int"),
    "ShipmentId": FieldSpec("secondary", "str"),
    "Shipment.DeviceId": FieldSpec("secondary", "str"),
}


class ParsedView:
    """Read access to parsed values by dotted path."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._invalid: set[str] = set()
        self._present: set[str] = set()
        self._containers: dict[str, str] = {}

    def get(self, path: str) -> Any:
        return self._values.get(path)

    def is_valid(self, path: str) -> bool:
        return path not in self._invalid

    def present(self, path: str) -> bool:
        """True when the key was in the payload (even with a null value)."""
        return path in self._present

    def has_value(self, path: str) -> bool:
        return (
            path in self._present
            and path not in self._invalid
            and self._values.get(path) is not None
        )

    def container_state(self, path: str) -> str:
        """'ok' (an object), 'absent' (missing or null) or 'invalid' (wrong type)."""
        return self._containers.get(path, "absent")


class _Fail(Exception):
    def __init__(self, code: IssueCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_utc_ms(text: str) -> int:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _Fail(IssueCode.TYPE_ERROR, "not an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise _Fail(IssueCode.TYPE_ERROR, "date-time has no timezone")
    return round(parsed.astimezone(UTC).timestamp() * 1000)


def _number(path: str, value: Any, spec: FieldSpec, issues: list[Issue]) -> float | int | None:
    """Return a number for a numeric field, or raise _Fail."""
    if isinstance(value, bool):
        raise _Fail(IssueCode.TYPE_ERROR, "booleans are not numbers")
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise _Fail(IssueCode.TYPE_ERROR, "number is not finite")
        return value
    if isinstance(value, str):
        if spec.group == "strict":
            raise _Fail(IssueCode.TYPE_ERROR, "expected a number, got a string")
        if not _NUMBER_RE.match(value.strip()):
            raise _Fail(IssueCode.UNPARSEABLE_NUMBER, f"{value!r} is not a number")
        converted = float(value.strip())
        if not math.isfinite(converted):
            raise _Fail(IssueCode.UNPARSEABLE_NUMBER, f"{value!r} is not finite")
        issues.append(
            Issue(
                IssueCode.NUMBER_FROM_STRING,
                path,
                Severity.CORRECTION,
                "numeric string converted to a number",
                original=value,
                corrected=converted,
            )
        )
        return converted
    raise _Fail(IssueCode.TYPE_ERROR, f"expected a number, got {type(value).__name__}")


def _parse_value(path: str, value: Any, spec: FieldSpec, issues: list[Issue]) -> Any:
    if value is None:
        return None
    if spec.kind == "str":
        if not isinstance(value, str):
            raise _Fail(IssueCode.TYPE_ERROR, f"expected a string, got {type(value).__name__}")
        return value
    if spec.kind == "datetime":
        if not isinstance(value, str):
            raise _Fail(IssueCode.TYPE_ERROR, f"expected a string, got {type(value).__name__}")
        return _parse_utc_ms(value)
    if spec.kind == "method":
        if not isinstance(value, str):
            raise _Fail(IssueCode.TYPE_ERROR, f"expected a string, got {type(value).__name__}")
        if value not in LOCATION_SOURCE_MAP:
            raise _Fail(IssueCode.UNKNOWN_LOCATION_METHOD, f"unknown location method {value!r}")
        return value
    number = _number(path, value, spec, issues)
    if number is None:
        return None
    if spec.kind == "int":
        if isinstance(number, float):
            if not number.is_integer():
                code = IssueCode.TYPE_ERROR if spec.group == "strict" else IssueCode.NOT_AN_INTEGER
                raise _Fail(code, f"{number} is not an integer")
            # the string-conversion correction (if any) already recorded the float
            return int(number)
        return number
    return number


def parse(raw: dict[str, Any]) -> tuple[ParsedView, list[Issue]]:
    view = ParsedView()
    issues: list[Issue] = []
    for path, spec in FIELDS.items():
        *containers, leaf = path.split(".")
        cur: Any = raw
        cpath = ""
        usable = True
        for name in containers:
            cpath = f"{cpath}.{name}" if cpath else name
            if not usable:
                break
            nxt = cur.get(name) if isinstance(cur, dict) else None
            if nxt is None:
                view._containers.setdefault(cpath, "absent")
                usable = False
            elif not isinstance(nxt, dict):
                if cpath not in view._containers:
                    view._containers[cpath] = "invalid"
                    if cpath not in _R0_CONTAINERS:
                        issues.append(
                            Issue(
                                IssueCode.TYPE_ERROR,
                                cpath,
                                Severity.WARNING,
                                f"expected an object, got {type(nxt).__name__}; "
                                "its fields are treated as absent",
                                original=nxt,
                            )
                        )
                usable = False
            else:
                view._containers[cpath] = "ok"
                cur = nxt
        if not usable or not isinstance(cur, dict) or leaf not in cur:
            continue
        view._present.add(path)
        original = cur[leaf]
        local: list[Issue] = []
        try:
            value = _parse_value(path, original, spec, local)
        except _Fail as fail:
            view._invalid.add(path)
            blocking = spec.group != "secondary"
            issues.append(
                Issue(
                    fail.code,
                    path,
                    Severity.BLOCKING if blocking else Severity.WARNING,
                    fail.message + ("" if blocking else "; value set to null"),
                    original=original,
                )
            )
            continue
        issues.extend(local)
        view._values[path] = value
    return view, issues
