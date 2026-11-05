"""Pipeline orchestration. Performs no I/O."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.transform import mapping
from app.transform.issues import Issue, IssueCode, Severity
from app.transform.mapping import SensorExtras
from app.transform.output_validation import validate_px
from app.transform.rules import run_rules
from app.transform.tive_fields import parse


@dataclass(frozen=True)
class PipelineResult:
    status_hint: str
    issues: list[Issue]
    px_sensor: dict[str, Any] | None
    px_location: dict[str, Any] | None
    extras: SensorExtras | None

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.BLOCKING]


_OVERRIDE_PATHS = {
    "device_imei": ("DeviceId",),
    "device_id": ("DeviceName",),
    "timestamp": ("EntryTimeEpoch",),
    "latitude": ("Location", "Latitude"),
    "longitude": ("Location", "Longitude"),
    "temperature": ("Temperature", "Celsius"),
}


def _apply_overrides(payload: dict[str, Any], overrides: Mapping[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for name, path in _OVERRIDE_PATHS.items():
        if name not in overrides:
            continue
        new = overrides[name]
        target = payload
        for key in path[:-1]:
            nxt = target.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                target[key] = nxt
            target = nxt
        original = target.get(path[-1])
        target[path[-1]] = new
        nulled = name == "temperature" and new is None
        issues.append(
            Issue(
                IssueCode.TEMPERATURE_NULLED_BY_OPERATOR if nulled else IssueCode.OPERATOR_OVERRIDE,
                ".".join(path),
                Severity.CORRECTION,
                "temperature set to null by operator" if nulled else f"{name} supplied by operator",
                original=original,
                corrected=new,
                source="operator",
            )
        )
    return issues


def transform(raw: Any, overrides: Mapping[str, Any] | None, now_ms: int) -> PipelineResult:
    if not isinstance(raw, dict):
        return PipelineResult(
            "rejected",
            [
                Issue(
                    IssueCode.NOT_AN_OBJECT,
                    "$",
                    Severity.BLOCKING,
                    "the JSON root must be an object",
                )
            ],
            None,
            None,
            None,
        )
    payload = copy.deepcopy(raw)
    issues: list[Issue] = []
    if overrides:
        issues.extend(_apply_overrides(payload, overrides))
    view, parse_issues = parse(payload)
    issues.extend(parse_issues)
    rule_issues, nullify, computed = run_rules(view, now_ms)
    issues.extend(rule_issues)
    px_sensor = mapping.to_px_sensor(view, nullify, computed)
    px_location = mapping.to_px_location(view, nullify)
    extras = mapping.extras(view, nullify)
    if not any(i.severity == Severity.BLOCKING for i in issues):
        issues.extend(validate_px("sensor", px_sensor))
        issues.extend(validate_px("location", px_location))
    if any(i.severity == Severity.BLOCKING for i in issues):
        hint = "quarantined"
    elif any(i.severity == Severity.CORRECTION for i in issues):
        hint = "accepted_with_corrections"
    else:
        hint = "accepted"
    return PipelineResult(hint, issues, px_sensor, px_location, extras)
