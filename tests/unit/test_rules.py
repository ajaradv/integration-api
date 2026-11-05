"""One class per rule R0-R12."""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from app.transform import constants as c
from app.transform.rules import Computed, Nullify, run_rules
from app.transform.tive_fields import parse

NOW = 1740787200000

BASE: dict[str, Any] = {
    "DeviceId": "863257063350583",
    "DeviceName": "A571992",
    "EntryTimeEpoch": 1739215646000,
    "Temperature": {"Celsius": 5.0},
    "Location": {"Latitude": 40.0, "Longitude": -73.0},
}


def run(**changes: Any) -> tuple[list[Any], list[Nullify], list[Computed]]:
    payload = copy.deepcopy(BASE)
    for key, value in changes.items():
        if value is ...:
            payload.pop(key, None)
        else:
            payload[key] = value
    view, _ = parse(payload)
    return run_rules(view, NOW)


def codes(issues: list[Any], severity: str | None = None) -> list[str]:
    return [str(i.code) for i in issues if severity is None or i.severity == severity]


def test_base_payload_is_clean() -> None:
    issues, nullify, computed = run()
    assert (issues, nullify, computed) == ([], [], [])


class TestR0:
    def test_temperature_missing(self) -> None:
        issues, _, _ = run(Temperature=...)
        assert [(str(i.code), i.field) for i in issues] == [
            ("missing_field", "Temperature"),
            ("missing_field", "Temperature.Celsius"),
        ]

    def test_temperature_not_object(self) -> None:
        issues, _, _ = run(Temperature="warm")
        assert codes(issues) == ["missing_field", "missing_field"]

    def test_celsius_key_missing(self) -> None:
        issues, _, _ = run(Temperature={"Fahrenheit": 41})
        assert [i.field for i in issues] == ["Temperature.Celsius"]

    def test_celsius_null_is_present(self) -> None:
        issues, _, _ = run(Temperature={"Celsius": None})
        assert codes(issues) == ["temperature_null"]  # R9, not R0

    def test_location_missing(self) -> None:
        issues, _, _ = run(Location=...)
        assert [i.field for i in issues] == ["Location", "Location.Latitude", "Location.Longitude"]
        assert set(codes(issues, "blocking")) == {"missing_field"}

    @pytest.mark.parametrize("key", ["Latitude", "Longitude"])
    def test_coordinate_missing_or_null(self, key: str) -> None:
        loc = {"Latitude": 1.0, "Longitude": 2.0}
        del loc[key]
        issues, _, _ = run(Location=loc)
        assert [i.field for i in issues] == [f"Location.{key}"]
        loc[key] = None
        issues, _, _ = run(Location=loc)
        assert [i.field for i in issues] == [f"Location.{key}"]

    def test_epoch_missing(self) -> None:
        issues, _, _ = run(EntryTimeEpoch=...)
        assert [(str(i.code), i.field) for i in issues] == [("missing_field", "EntryTimeEpoch")]

    def test_invalid_field_not_reported_twice(self) -> None:
        issues, _, _ = run(Location={"Latitude": "x", "Longitude": 1.0})
        assert issues == []  # the parser already reported the type_error


class TestR1:
    def test_missing(self) -> None:
        issues, _, _ = run(DeviceId=...)
        assert codes(issues) == ["device_imei_missing"]

    def test_null(self) -> None:
        issues, _, _ = run(DeviceId=None)
        assert codes(issues) == ["device_imei_missing"]

    @pytest.mark.parametrize("value", ["12345", "86325706335058", "8632570633505830", "A" * 15, ""])
    def test_malformed(self, value: str) -> None:
        issues, _, _ = run(DeviceId=value)
        assert codes(issues) == ["device_imei_malformed"]
        assert issues[0].original == value

    def test_trailing_newline_is_malformed(self) -> None:
        issues, _, _ = run(DeviceId="863257063350583\n")
        assert codes(issues) == ["device_imei_malformed"]

    def test_wrong_type_not_reported_again(self) -> None:
        issues, _, _ = run(DeviceId=5)
        assert issues == []


class TestR2:
    @pytest.mark.parametrize("value", [..., None, "", "   "])
    def test_missing_or_blank(self, value: Any) -> None:
        issues, _, _ = run(DeviceName=value)
        assert codes(issues) == ["device_id_missing"]

    def test_ok(self) -> None:
        assert run()[0] == []


class TestR3:
    def test_differs_is_warning(self) -> None:
        issues, _, _ = run(EntityName="B1")
        assert codes(issues) == ["entity_name_differs"]
        assert issues[0].severity == "warning"

    def test_same(self) -> None:
        assert run(EntityName="A571992")[0] == []

    def test_absent_no_warning(self) -> None:
        assert run()[0] == []

    def test_null_no_warning(self) -> None:
        assert run(EntityName=None)[0] == []


class TestR4:
    def test_mismatch(self) -> None:
        issues, _, _ = run(Shipment={"DeviceId": "866088073468439"})
        assert codes(issues) == ["shipment_device_mismatch"]

    def test_match(self) -> None:
        assert run(Shipment={"DeviceId": BASE["DeviceId"]})[0] == []

    @pytest.mark.parametrize("shipment", [None, {}, {"DeviceId": None}])
    def test_absent(self, shipment: Any) -> None:
        assert run(Shipment=shipment)[0] == []


class TestR5:
    def test_min_boundary(self) -> None:
        assert run(EntryTimeEpoch=c.MIN_TIMESTAMP_MS)[0] == []
        assert codes(run(EntryTimeEpoch=c.MIN_TIMESTAMP_MS - 1)[0]) == ["timestamp_implausible"]

    def test_future_boundary(self) -> None:
        assert run(EntryTimeEpoch=NOW + c.MAX_FUTURE_MS)[0] == []
        assert codes(run(EntryTimeEpoch=NOW + c.MAX_FUTURE_MS + 1)[0]) == ["timestamp_implausible"]

    def test_seconds_are_never_converted(self) -> None:
        issues, _, _ = run(EntryTimeEpoch=1739561246)
        assert codes(issues) == ["timestamp_implausible"]
        assert issues[0].corrected is None

    def test_negative(self) -> None:
        assert codes(run(EntryTimeEpoch=-5)[0]) == ["timestamp_implausible"]


class TestR6:
    def test_agree(self) -> None:
        assert run(EntryTimeUtc="2025-02-10T19:27:26Z")[0] == []

    def test_within_tolerance(self) -> None:
        assert run(EntryTimeUtc="2025-02-10T19:27:27Z")[0] == []

    def test_beyond_tolerance(self) -> None:
        assert codes(run(EntryTimeUtc="2025-02-10T19:27:28Z")[0]) == ["timestamp_mismatch"]

    def test_hours_apart_is_warning_only(self) -> None:
        issues = run(EntryTimeUtc="2025-02-10T14:27:26Z")[0]
        assert [(str(i.code), i.severity) for i in issues] == [("timestamp_mismatch", "warning")]

    def test_absent(self) -> None:
        assert run()[0] == []

    def test_reported_alongside_r5(self) -> None:
        issues, _, _ = run(EntryTimeEpoch=1739561246, EntryTimeUtc="2025-02-14T19:27:26Z")
        assert codes(issues) == ["timestamp_implausible", "timestamp_mismatch"]


class TestR7:
    @pytest.mark.parametrize("lat", [90.0, -90.0, 0.0])
    def test_lat_ok(self, lat: float) -> None:
        assert run(Location={"Latitude": lat, "Longitude": 0.0})[0] == []

    @pytest.mark.parametrize("lat", [90.000001, -90.000001, 95.0])
    def test_lat_bad(self, lat: float) -> None:
        issues, _, _ = run(Location={"Latitude": lat, "Longitude": 0.0})
        assert codes(issues) == ["latitude_out_of_range"]

    @pytest.mark.parametrize("lon", [180.0, -180.0])
    def test_lon_ok(self, lon: float) -> None:
        assert run(Location={"Latitude": 0.0, "Longitude": lon})[0] == []

    @pytest.mark.parametrize("lon", [180.000001, -200.0])
    def test_lon_bad(self, lon: float) -> None:
        issues, _, _ = run(Location={"Latitude": 0.0, "Longitude": lon})
        assert codes(issues) == ["longitude_out_of_range"]

    def test_both_reported(self) -> None:
        issues, _, _ = run(Location={"Latitude": 95.0, "Longitude": -200.0})
        assert codes(issues) == ["latitude_out_of_range", "longitude_out_of_range"]


class TestR8:
    @pytest.mark.parametrize("value", [*c.OBSERVED_FAULT_VALUES, 100.01, -100.01])
    def test_fault_values_block(self, value: float) -> None:
        issues, _, _ = run(Temperature={"Celsius": value})
        assert codes(issues, "blocking") == ["temperature_fault"]

    @pytest.mark.parametrize("value", [100.0, -100.0, -18.5, 0])
    def test_in_range(self, value: float) -> None:
        assert run(Temperature={"Celsius": value})[0] == []


class TestR9:
    def test_null_is_warning(self) -> None:
        issues, _, _ = run(Temperature={"Celsius": None})
        assert [(str(i.code), i.severity) for i in issues] == [("temperature_null", "warning")]


class TestR10:
    @pytest.mark.parametrize("value", [*c.OBSERVED_FAULT_VALUES, 100.5, -100.5])
    def test_fault(self, value: float) -> None:
        issues, nullify, _ = run(ProbeTemperature={"Celsius": value})
        assert [(str(i.code), i.severity) for i in issues] == [("probe_fault", "warning")]
        assert nullify == [Nullify("probe_temperature", fault=True)]

    def test_ok_and_null(self) -> None:
        assert run(ProbeTemperature={"Celsius": 100.0}) == ([], [], [])
        assert run(ProbeTemperature={"Celsius": None}) == ([], [], [])
        assert run(ProbeTemperature=None) == ([], [], [])


class TestR11:
    def test_humidity(self) -> None:
        assert run(Humidity={"Percentage": 100})[0] == []
        assert run(Humidity={"Percentage": 0})[0] == []
        issues, nullify, _ = run(Humidity={"Percentage": 100.1})
        assert codes(issues) == ["humidity_out_of_range"] and nullify == [Nullify("humidity")]
        assert codes(run(Humidity={"Percentage": -0.1})[0]) == ["humidity_out_of_range"]

    def test_lux(self) -> None:
        assert run(Light={"Lux": 0})[0] == []
        issues, nullify, _ = run(Light={"Lux": -1})
        assert codes(issues) == ["lux_out_of_range"] and nullify == [Nullify("light_level")]

    def test_battery(self) -> None:
        assert run(Battery={"Percentage": 100})[0] == []
        assert run(Battery={"Percentage": 0})[0] == []
        issues, nullify, _ = run(Battery={"Percentage": 101})
        assert codes(issues) == ["battery_out_of_range"] and nullify == [Nullify("battery_level")]
        assert codes(run(Battery={"Percentage": -1})[0]) == ["battery_out_of_range"]

    def test_dbm_boundaries(self) -> None:
        assert run(Cellular={"Dbm": -150})[0] == []
        assert run(Cellular={"Dbm": -50})[0] == []
        issues, nullify, _ = run(Cellular={"Dbm": -151})
        assert codes(issues) == ["dbm_out_of_range"] and nullify == [Nullify("cellular_dbm")]
        assert codes(run(Cellular={"Dbm": -49})[0]) == ["dbm_out_of_range"]

    def test_accuracy(self) -> None:
        loc = {"Latitude": 1.0, "Longitude": 2.0}
        assert run(Location={**loc, "Accuracy": {"Meters": 0}})[0] == []
        issues, nullify, _ = run(Location={**loc, "Accuracy": {"Meters": -3}})
        assert codes(issues) == ["accuracy_out_of_range"]
        assert nullify == [Nullify("location_accuracy")]

    def test_out_of_range_is_warning_not_blocking(self) -> None:
        issues, _, _ = run(Humidity={"Percentage": 500})
        assert codes(issues, "blocking") == []


class TestR12:
    def test_magnitude_computed(self) -> None:
        acc = {"G": None, "X": 3.0, "Y": 4.0, "Z": 12.0}
        issues, _, computed = run(Accelerometer=acc)
        assert [(str(i.code), i.severity) for i in issues] == [("magnitude_computed", "correction")]
        assert computed == [Computed("magnitude", 13.0)]
        assert math.isclose(issues[0].corrected, 13.0)

    def test_g_absent_key(self) -> None:
        _, _, computed = run(Accelerometer={"X": 0.0, "Y": 0.0, "Z": 1.0})
        assert computed == [Computed("magnitude", 1.0)]

    def test_g_present(self) -> None:
        assert run(Accelerometer={"G": 1.0, "X": 0.0, "Y": 0.0, "Z": 1.0}) == ([], [], [])

    def test_axis_missing(self) -> None:
        assert run(Accelerometer={"G": None, "X": 0.0, "Y": 0.0}) == ([], [], [])

    def test_accelerometer_absent(self) -> None:
        assert run(Accelerometer=None) == ([], [], [])
