from __future__ import annotations

import json
from typing import Any

import pytest

from app.transform import constants as c
from app.transform.mapping import accuracy_category, extras, to_px_location, to_px_sensor
from app.transform.output_validation import SCHEMA_DIR
from app.transform.rules import Computed, Nullify
from app.transform.tive_fields import parse


def example(kind: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"px-{kind}-schema.json").read_text())["examples"][0]


def view_of(payload: dict[str, Any]) -> Any:
    return parse(payload)[0]


def test_sample_1_matches_px_sensor_example_field_for_field(samples: list[dict[str, Any]]) -> None:
    sensor = to_px_sensor(view_of(samples[0]), [], [])
    expected = example("sensor")
    assert sensor == expected
    assert list(sensor) == list(expected)  # same key order too


def test_sample_1_matches_px_location_example_field_for_field(
    samples: list[dict[str, Any]],
) -> None:
    location = to_px_location(view_of(samples[0]), [])
    expected = example("location")
    assert location == expected
    assert list(location) == list(expected)
    assert location["address"] is not None and location["address"]["street"] is None


def test_extras_sample_1(samples: list[dict[str, Any]]) -> None:
    e = extras(view_of(samples[0]), [])
    assert e.probe_temperature == 9.94
    assert e.probe_fault is False
    assert e.account_id == 478
    assert e.entity_name == "A571992"
    assert e.shipment_id == "CL-13686/PHARMA-SHIP/COLD-LOGISTICS"


@pytest.mark.parametrize(
    ("method", "expected"), [("gps", "GPS"), ("wifi", "WiFi"), ("cell", "Cellular"), (None, None)]
)
def test_location_source(method: str | None, expected: str | None) -> None:
    payload = {"Location": {"Latitude": 1.0, "Longitude": 2.0, "LocationMethod": method}}
    assert to_px_location(view_of(payload), [])["location_source"] == expected
    assert set(c.LOCATION_SOURCE_MAP.values()) == {"GPS", "WiFi", "Cellular"}


@pytest.mark.parametrize(
    ("meters", "category"),
    [(0, "High"), (50, "High"), (51, "Medium"), (500, "Medium"), (501, "Low"), (None, None)],
)
def test_accuracy_thresholds(meters: float | None, category: str | None) -> None:
    assert accuracy_category(meters) == category
    payload = {"Location": {"Latitude": 1.0, "Longitude": 2.0, "Accuracy": {"Meters": meters}}}
    location = to_px_location(view_of(payload), [])
    assert location["location_accuracy_category"] == category
    assert location["location_accuracy"] == (None if meters is None else round(meters))


def test_missing_accelerometer_is_null() -> None:
    assert to_px_sensor(view_of({"Temperature": {"Celsius": 1}}), [], [])["accelerometer"] is None


def test_partial_accelerometer_keeps_object_with_nulls() -> None:
    sensor = to_px_sensor(view_of({"Accelerometer": {"X": 0.5}}), [], [])
    assert sensor["accelerometer"] == {"x": 0.5, "y": None, "z": None, "magnitude": None}


def test_computed_magnitude_is_used_and_rounded() -> None:
    payload = {"Accelerometer": {"X": 3.0, "Y": 4.0, "Z": 12.0}}
    sensor = to_px_sensor(view_of(payload), [], [Computed("magnitude", 13.0004)])
    assert sensor["accelerometer"]["magnitude"] == 13.0


def test_nullify_markers_are_honored() -> None:
    payload = {
        "Humidity": {"Percentage": 50},
        "Light": {"Lux": 5},
        "Battery": {"Percentage": 50},
        "Cellular": {"Dbm": -80},
        "ProbeTemperature": {"Celsius": 5},
        "Location": {"Latitude": 1.0, "Longitude": 2.0, "Accuracy": {"Meters": 10}},
    }
    nulls = [
        Nullify("humidity"),
        Nullify("light_level"),
        Nullify("battery_level"),
        Nullify("cellular_dbm"),
        Nullify("location_accuracy"),
        Nullify("probe_temperature", fault=True),
    ]
    view = view_of(payload)
    sensor = to_px_sensor(view, nulls, [])
    location = to_px_location(view, nulls)
    assert sensor["humidity"] is None and sensor["light_level"] is None
    assert location["battery_level"] is None and location["cellular_dbm"] is None
    assert location["location_accuracy"] is None
    assert location["location_accuracy_category"] is None
    e = extras(view, nulls)
    assert e.probe_temperature is None and e.probe_fault is True


def test_provider_and_type_constants() -> None:
    sensor = to_px_sensor(view_of({}), [], [])
    assert (sensor["provider"], sensor["type"]) == ("Tive", "Active")
    assert sensor["tilt"] is None and sensor["box_open"] is None


def test_rounding_in_mapping() -> None:
    payload = {
        "Temperature": {"Celsius": 2.675},
        "Humidity": {"Percentage": 38.75},
        "Light": {"Lux": 125.55},
        "Cellular": {"Dbm": -75.555},
    }
    view = view_of(payload)
    sensor = to_px_sensor(view, [], [])
    assert (sensor["temperature"], sensor["humidity"], sensor["light_level"]) == (2.68, 38.8, 125.6)
    assert to_px_location(view, [])["cellular_dbm"] == -75.56


def test_address_null_when_formatted_address_null() -> None:
    payload = {"Location": {"FormattedAddress": None}}
    assert to_px_location(view_of(payload), [])["address"] is None
