from typing import Any

import pytest

from app.transform.tive_fields import parse


def codes(issues: list[Any]) -> list[tuple[str, str, str]]:
    return [(str(i.code), i.field, str(i.severity)) for i in issues]


def test_numeric_string_in_measurement_is_converted_with_correction() -> None:
    view, issues = parse({"Temperature": {"Celsius": "10.5", "Fahrenheit": "50.9"}})
    assert view.get("Temperature.Celsius") == 10.5
    assert view.get("Temperature.Fahrenheit") == 50.9
    assert codes(issues) == [
        ("number_from_string", "Temperature.Celsius", "correction"),
        ("number_from_string", "Temperature.Fahrenheit", "correction"),
    ]
    assert issues[0].original == "10.5"


@pytest.mark.parametrize("text", ["1e1", ".5", "-3.", "+2"])
def test_numeric_string_forms(text: str) -> None:
    view, _ = parse({"Humidity": {"Percentage": text}})
    assert view.get("Humidity.Percentage") == float(text)


@pytest.mark.parametrize("text", ["abc", "", "1,5", "NaN", "inf", "1e999", "0x10"])
def test_unparseable_secondary_is_warning_and_null(text: str) -> None:
    view, issues = parse({"Humidity": {"Percentage": text}})
    assert view.get("Humidity.Percentage") is None
    assert not view.is_valid("Humidity.Percentage")
    assert codes(issues) == [("unparseable_number", "Humidity.Percentage", "warning")]


def test_unparseable_primary_temperature_is_blocking() -> None:
    view, issues = parse({"Temperature": {"Celsius": "hot"}})
    assert not view.is_valid("Temperature.Celsius")
    assert codes(issues) == [("unparseable_number", "Temperature.Celsius", "blocking")]


def test_primary_wrong_type_is_blocking() -> None:
    _, issues = parse({"Temperature": {"Celsius": [1]}})
    assert codes(issues) == [("type_error", "Temperature.Celsius", "blocking")]


@pytest.mark.parametrize(
    "raw",
    [
        {"Temperature": {"Celsius": True}},
        {"Humidity": {"Percentage": False}},
        {"Location": {"Latitude": True}},
    ],
)
def test_booleans_are_not_numbers(raw: dict[str, Any]) -> None:
    view, issues = parse(raw)
    assert len(issues) == 1
    assert str(issues[0].code) == "type_error"
    assert all(not view.has_value(p) for p in ("Temperature.Celsius", "Humidity.Percentage"))


@pytest.mark.parametrize(
    "raw",
    [
        {"Location": {"Latitude": "40.0"}},
        {"Location": {"Longitude": "-73.0"}},
        {"EntryTimeEpoch": "1739215646000"},
        {"DeviceId": 863257063350583},
        {"DeviceName": 7},
        {"EntityName": ["x"]},
        {"EntryTimeUtc": 5},
        {"EntryTimeUtc": "yesterday"},
        {"EntryTimeUtc": "2025-02-10T19:27:26"},
    ],
)
def test_strict_fields_reject_wrong_types(raw: dict[str, Any]) -> None:
    view, issues = parse(raw)
    assert len(issues) == 1
    assert issues[0].severity == "blocking"
    assert str(issues[0].code) == "type_error"
    path = issues[0].field
    assert not view.is_valid(path)


def test_strict_integer_accepts_integral_float_only() -> None:
    view, issues = parse({"EntryTimeEpoch": 1739215646000.0})
    assert view.get("EntryTimeEpoch") == 1739215646000 and not issues
    _, issues = parse({"EntryTimeEpoch": 1.5})
    assert codes(issues) == [("type_error", "EntryTimeEpoch", "blocking")]


def test_one_invalid_strict_field_does_not_stop_the_others() -> None:
    view, issues = parse(
        {
            "DeviceId": 12,
            "DeviceName": "A1",
            "Location": {"Latitude": "x", "Longitude": -73.0},
            "Humidity": {"Percentage": 40},
        }
    )
    assert view.get("DeviceName") == "A1"
    assert view.get("Location.Longitude") == -73.0
    assert view.get("Humidity.Percentage") == 40
    assert {i.field for i in issues} == {"DeviceId", "Location.Latitude"}


def test_non_integer_for_integer_field_is_warning_and_null() -> None:
    view, issues = parse(
        {"Battery": {"Percentage": 65.5}, "Location": {"WifiAccessPointUsedCount": 2.5}}
    )
    assert view.get("Battery.Percentage") is None
    assert view.get("Location.WifiAccessPointUsedCount") is None
    assert codes(issues) == [
        ("not_an_integer", "Battery.Percentage", "warning"),
        ("not_an_integer", "Location.WifiAccessPointUsedCount", "warning"),
    ]


def test_integral_float_and_string_for_integer_field() -> None:
    view, issues = parse({"Battery": {"Percentage": 65.0}, "AccountId": "478"})
    assert view.get("Battery.Percentage") == 65
    assert isinstance(view.get("Battery.Percentage"), int)
    assert view.get("AccountId") == 478
    assert codes(issues) == [("number_from_string", "AccountId", "correction")]


def test_absent_vs_null_vs_invalid() -> None:
    view, _ = parse({"Temperature": {"Celsius": None}, "Humidity": {"Percentage": "x"}})
    assert view.present("Temperature.Celsius") and view.is_valid("Temperature.Celsius")
    assert not view.has_value("Temperature.Celsius")
    assert not view.present("Light.Lux")
    assert view.present("Humidity.Percentage") and not view.is_valid("Humidity.Percentage")


def test_container_wrong_type_is_warning_for_optional_containers() -> None:
    view, issues = parse({"Humidity": "wet", "Temperature": "warm", "Cellular": None})
    assert codes(issues) == [("type_error", "Humidity", "warning")]
    assert view.container_state("Humidity") == "invalid"
    assert view.container_state("Temperature") == "invalid"  # R0 reports this one
    assert view.container_state("Cellular") == "absent"


def test_location_method_enum() -> None:
    view, issues = parse({"Location": {"LocationMethod": "gps"}})
    assert view.get("Location.LocationMethod") == "gps" and not issues
    view, issues = parse({"Location": {"LocationMethod": "sonar"}})
    assert view.get("Location.LocationMethod") is None
    assert codes(issues) == [("unknown_location_method", "Location.LocationMethod", "warning")]


def test_utc_time_parsed_to_ms() -> None:
    view, issues = parse({"EntryTimeUtc": "2025-02-10T19:27:26Z"})
    assert view.get("EntryTimeUtc") == 1739215646000 and not issues
    view, _ = parse({"EntryTimeUtc": "2025-02-10T20:27:26+01:00"})
    assert view.get("EntryTimeUtc") == 1739215646000
