"""The main accuracy test: all 13 provided samples against the expected outcomes."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.transform import transform
from app.transform.output_validation import validate_px
from tests.fixtures.expected_outcomes import EXPECTED

NOW = 1740787200000


def codes(result: Any, severity: str) -> list[str]:
    return [str(i.code) for i in result.issues if i.severity == severity]


@pytest.mark.parametrize("index", sorted(EXPECTED))
def test_sample_outcome(index: int, samples: list[dict[str, Any]]) -> None:
    expected = EXPECTED[index]
    result = transform(samples[index - 1], None, NOW)

    assert result.status_hint == expected["status"]
    assert codes(result, "blocking") == expected["blocking"]
    assert sorted(codes(result, "warning")) == sorted(expected["warning"])
    assert codes(result, "correction") == expected["correction"]

    assert result.px_sensor is not None and result.px_location is not None
    assert result.extras is not None
    if result.status_hint != "quarantined":
        assert validate_px("sensor", result.px_sensor) == []
        assert validate_px("location", result.px_location) == []

    for key, value in expected.get("sensor", {}).items():
        assert result.px_sensor[key] == value, key
    for key, value in expected.get("location", {}).items():
        assert result.px_location[key] == value, key
    if "magnitude" in expected:
        assert result.px_sensor["accelerometer"]["magnitude"] == expected["magnitude"]
    if "probe" in expected:
        assert (result.extras.probe_temperature, result.extras.probe_fault) == expected["probe"]
    if "account_id" in expected:
        assert result.extras.account_id == expected["account_id"]


def test_quarantined_set_matches_design_table(
    samples: list[dict[str, Any]],
) -> None:
    statuses = [transform(s, None, NOW).status_hint for s in samples]
    quarantined = [i + 1 for i, s in enumerate(statuses) if s == "quarantined"]
    assert quarantined == [2, 5, 6, 9, 13]


@pytest.mark.parametrize("root", [[], [1, 2], "text", 5, 1.5, True, None])
def test_non_object_roots_are_rejected(root: Any) -> None:
    result = transform(root, None, NOW)
    assert result.status_hint == "rejected"
    assert [str(i.code) for i in result.issues] == ["not_an_object"]
    assert result.px_sensor is None and result.px_location is None


def test_string_latitude_still_reports_other_issues_and_builds_candidate(
    samples: list[dict[str, Any]],
) -> None:
    payload = copy.deepcopy(samples[0])
    payload["Location"]["Latitude"] = "40.8"
    del payload["DeviceId"]
    result = transform(payload, None, NOW)
    assert result.status_hint == "quarantined"
    assert sorted(codes(result, "blocking")) == ["device_imei_missing", "type_error"]
    assert result.px_sensor is not None
    assert result.px_sensor["temperature"] == 10.08
    assert result.px_location is not None and result.px_location["latitude"] is None


def test_sample_2_with_latitude_override_has_no_blocking(samples: list[dict[str, Any]]) -> None:
    result = transform(samples[1], {"latitude": 40.810562}, NOW)
    assert codes(result, "blocking") == []
    assert result.status_hint == "accepted_with_corrections"
    override = [i for i in result.issues if str(i.code) == "operator_override"]
    assert len(override) == 1
    assert override[0].source == "operator"
    assert (override[0].original, override[0].corrected) == (95.0, 40.810562)
    assert result.px_location is not None and result.px_location["latitude"] == 40.810562
    assert samples[1]["Location"]["Latitude"] == 95.0  # input not mutated


def test_sample_5_with_imei_override(samples: list[dict[str, Any]]) -> None:
    result = transform(samples[4], {"device_imei": "866088073468499"}, NOW)
    assert codes(result, "blocking") == []
    assert result.px_sensor is not None and result.px_sensor["device_imei"] == "866088073468499"


def test_malformed_override_imei_stays_blocking(samples: list[dict[str, Any]]) -> None:
    result = transform(samples[4], {"device_imei": "123"}, NOW)
    assert codes(result, "blocking") == ["device_imei_malformed"]


def test_temperature_null_override(samples: list[dict[str, Any]]) -> None:
    payload = copy.deepcopy(samples[0])
    payload["Temperature"]["Celsius"] = 327.67
    assert transform(payload, None, NOW).status_hint == "quarantined"
    result = transform(payload, {"temperature": None}, NOW)
    assert codes(result, "blocking") == []
    assert "temperature_nulled_by_operator" in [str(i.code) for i in result.issues]
    assert "temperature_null" in codes(result, "warning")
    assert result.px_sensor is not None and result.px_sensor["temperature"] is None


def test_sample_6_timestamp_override_fixes_both_timestamp_issues(
    samples: list[dict[str, Any]],
) -> None:
    result = transform(samples[5], {"timestamp": 1739561246000}, NOW)
    assert codes(result, "blocking") == []


def test_overrides_create_missing_containers() -> None:
    result = transform({"DeviceName": "x"}, {"latitude": 1.0, "longitude": 2.0}, NOW)
    assert "missing_field" in codes(result, "blocking")
    assert result.px_location is not None and result.px_location["latitude"] == 1.0


def test_probe_fault_payload_keeps_primary_reading(samples: list[dict[str, Any]]) -> None:
    result = transform(samples[3], None, NOW)
    assert result.px_sensor is not None and result.px_sensor["temperature"] == 4.63
    assert result.extras is not None and result.extras.probe_fault is True
