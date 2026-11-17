"""R13: location continuity against the neighbouring stored readings."""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

import pytest

from app.transform import Neighbour, apply_continuity, transform
from app.transform import constants as c
from app.transform.continuity import check_location_jump, checkable, haversine_m

NOW = 1740787200000
HOUR_MS = 3_600_000

SF = (37.7749, -122.4194)
RICHMOND = (37.5407, -77.436)


def px(lat: float, lon: float, ts: int = 1739305646000, accuracy: int | None = 5) -> dict[str, Any]:
    return {
        "device_imei": "866088073468440",
        "timestamp": ts,
        "latitude": lat,
        "longitude": lon,
        "location_accuracy": accuracy,
    }


def neighbour(
    lat: float, lon: float, ts: int = 1739302046000, accuracy: float | None = 5
) -> Neighbour:
    return Neighbour(uuid4(), ts, lat, lon, accuracy)


class TestHaversine:
    def test_zero_for_same_point(self) -> None:
        assert haversine_m(*SF, *SF) == 0.0

    def test_sf_to_richmond(self) -> None:
        assert haversine_m(*SF, *RICHMOND) == pytest.approx(3_920_624, rel=1e-4)

    def test_one_degree_of_latitude(self) -> None:
        assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-3)

    def test_antimeridian_is_short_way_round(self) -> None:
        # 179.9 E to 179.9 W is 0.2 degrees of longitude at the equator, not 359.8
        assert haversine_m(0, 179.9, 0, -179.9) == pytest.approx(22_239, rel=1e-3)

    def test_symmetric(self) -> None:
        assert haversine_m(*SF, *RICHMOND) == pytest.approx(haversine_m(*RICHMOND, *SF))


class TestCheckLocationJump:
    def test_sample_3_to_4_is_flagged_blocking(self) -> None:
        prev = neighbour(*SF)
        issues = check_location_jump(px(*RICHMOND), prev, None)
        assert len(issues) == 1
        issue = issues[0]
        assert (str(issue.code), str(issue.severity), issue.field) == (
            "location_jump",
            "blocking",
            "Location",
        )
        assert issue.original["neighbour"] == "previous"
        assert issue.original["ingestion_id"] == str(prev.ingestion_id)
        assert issue.original["elapsed_s"] == 3600
        assert issue.original["implied_speed_kmh"] == pytest.approx(3920.6, abs=0.5)
        assert issue.original["max_speed_kmh"] == 1000.0

    def test_sample_4_to_12_is_plausible(self) -> None:
        # Richmond -> Charlotte, 399 km in 95 h
        prev = neighbour(*RICHMOND, ts=1739305646000)
        assert check_location_jump(px(35.2271, -80.8431, ts=1739647646000), prev, None) == []

    def test_movement_within_gps_error_passes(self) -> None:
        # samples 1 and 8: 45 m apart, accuracy 23 m and 25 m
        prev = neighbour(40.810562, -73.879285, ts=1739215646000, accuracy=23)
        new = px(40.81091, -73.87902, ts=1739216546000, accuracy=25)
        assert check_location_jump(new, prev, None) == []

    def test_boundary_exactly_at_allowed_distance_passes(self) -> None:
        # place the new fix exactly v_max * dt + floor + accuracies away along a meridian
        dt_s = 3600
        allowed = c.LOCATION_JUMP_MAX_SPEED_MPS * dt_s + c.LOCATION_JUMP_FLOOR_M + 10
        metres_per_degree = haversine_m(0, 0, 1, 0)
        lat_ok = (allowed - 1) / metres_per_degree
        lat_bad = (allowed + 50) / metres_per_degree
        prev = neighbour(0, 0, ts=0)
        assert check_location_jump(px(lat_ok, 0, ts=dt_s * 1000), prev, None) == []
        assert len(check_location_jump(px(lat_bad, 0, ts=dt_s * 1000), prev, None)) == 1

    def test_floor_absorbs_short_gaps(self) -> None:
        # 900 m in 1 s would be 3,240 km/h, but stays inside the 1 km floor
        prev = neighbour(0, 0, ts=0, accuracy=0)
        lat = 900 / haversine_m(0, 0, 1, 0)
        assert check_location_jump(px(lat, 0, ts=1000, accuracy=0), prev, None) == []

    def test_missing_accuracy_uses_default(self) -> None:
        # 1,900 m in 1 s: flagged with 0 m accuracy, absorbed by 2 x 500 m default
        lat = 1900 / haversine_m(0, 0, 1, 0)
        precise = neighbour(0, 0, ts=0, accuracy=0)
        assert len(check_location_jump(px(lat, 0, ts=1000, accuracy=0), precise, None)) == 1
        unknown = neighbour(0, 0, ts=0, accuracy=None)
        assert check_location_jump(px(lat, 0, ts=1000, accuracy=None), unknown, None) == []

    def test_checks_the_following_reading_too(self) -> None:
        # a backfilled reading between two stored ones is checked against both
        prev = neighbour(*SF, ts=1739302046000)
        following = neighbour(*SF, ts=1739302046000 + 2 * HOUR_MS)
        new = px(*RICHMOND, ts=1739302046000 + HOUR_MS)
        issues = check_location_jump(new, prev, following)
        assert [i.original["neighbour"] for i in issues] == ["previous", "next"]

    def test_only_the_implausible_side_is_reported(self) -> None:
        prev = neighbour(*SF, ts=1739302046000)
        following = neighbour(*RICHMOND, ts=1739302046000 + 2 * HOUR_MS)
        new = px(*RICHMOND, ts=1739302046000 + HOUR_MS)
        issues = check_location_jump(new, prev, following)
        assert [i.original["neighbour"] for i in issues] == ["previous"]

    def test_no_neighbours_no_issues(self) -> None:
        assert check_location_jump(px(*RICHMOND), None, None) == []

    def test_same_timestamp_neighbour_is_ignored(self) -> None:
        # same (device, timestamp) is a replacement, handled by the upsert, not R13
        prev = neighbour(*SF, ts=1739305646000)
        assert check_location_jump(px(*RICHMOND, ts=1739305646000), prev, None) == []

    def test_acknowledged_becomes_operator_warning(self) -> None:
        issues = check_location_jump(px(*RICHMOND), neighbour(*SF), None, acknowledged=True)
        assert [(str(i.severity), i.source) for i in issues] == [("warning", "operator")]
        assert "acknowledged by operator" in issues[0].message


class TestCheckable:
    @pytest.mark.parametrize(
        "change",
        [
            {"device_imei": None},
            {"device_imei": "123"},
            {"timestamp": None},
            {"timestamp": True},
            {"latitude": None},
            {"latitude": 95.0},
            {"longitude": -200.0},
            {"longitude": "1.0"},
        ],
    )
    def test_not_checkable(self, change: dict[str, Any]) -> None:
        assert not checkable({**px(*SF), **change})
        assert check_location_jump({**px(*SF), **change}, neighbour(*RICHMOND), None) == []

    def test_none(self) -> None:
        assert not checkable(None)


class TestApplyContinuity:
    @pytest.fixture
    def sample_4(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        return copy.deepcopy(samples[3])

    def test_flags_and_quarantines(self, sample_4: dict[str, Any]) -> None:
        result = transform(sample_4, None, NOW)
        assert result.status_hint == "accepted"
        out = apply_continuity(result, neighbour(*SF), None)
        assert out.status_hint == "quarantined"
        assert [str(i.code) for i in out.blocking] == ["location_jump"]
        assert len(out.issues) == len(result.issues) + 1
        assert out.px_sensor == result.px_sensor and out.px_location == result.px_location

    def test_acknowledged_keeps_status(self, sample_4: dict[str, Any]) -> None:
        result = transform(sample_4, None, NOW)
        out = apply_continuity(result, neighbour(*SF), None, acknowledged=True)
        assert out.status_hint == "accepted"
        assert "location_jump" in [str(i.code) for i in out.issues]

    def test_plausible_returns_same_result(self, sample_4: dict[str, Any]) -> None:
        result = transform(sample_4, None, NOW)
        assert apply_continuity(result, neighbour(*RICHMOND), None) is result

    def test_rejected_is_untouched(self) -> None:
        result = transform([1, 2], None, NOW)
        assert apply_continuity(result, neighbour(*SF), None) is result

    def test_without_imei_is_untouched(self, samples: list[dict[str, Any]]) -> None:
        # sample 5: no IMEI, so R13 cannot run
        result = transform(samples[4], None, NOW)
        assert apply_continuity(result, neighbour(*SF), None) is result
