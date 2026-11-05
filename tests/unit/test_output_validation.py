import copy
import json
from pathlib import Path

from app.transform.output_validation import SCHEMA_DIR, validate_px


def example(kind: str) -> dict:
    schema = json.loads(Path(SCHEMA_DIR / f"px-{kind}-schema.json").read_text())
    return copy.deepcopy(schema["examples"][0])


def test_valid_records_pass() -> None:
    assert validate_px("sensor", example("sensor")) == []
    assert validate_px("location", example("location")) == []


def test_missing_required_field() -> None:
    record = example("sensor")
    del record["temperature"]
    issues = validate_px("sensor", record)
    assert [str(i.code) for i in issues] == ["output_schema_violation"]
    assert issues[0].severity == "blocking"
    assert "temperature" in issues[0].message


def test_bad_enum_value_has_pointer() -> None:
    record = example("sensor")
    record["provider"] = "Nope"
    issues = validate_px("sensor", record)
    assert issues[0].field == "px_sensor/provider"


def test_battery_over_100() -> None:
    record = example("location")
    record["battery_level"] = 101
    issues = validate_px("location", record)
    assert [i.field for i in issues] == ["px_location/battery_level"]
    assert issues[0].original == 101


def test_wrong_type_nested() -> None:
    record = example("sensor")
    record["accelerometer"]["x"] = "high"
    issues = validate_px("sensor", record)
    assert issues[0].field == "px_sensor/accelerometer/x"
