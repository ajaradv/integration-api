"""Issue model shared by the parser, the rules and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    CORRECTION = "correction"
    WARNING = "warning"
    BLOCKING = "blocking"


class IssueCode(StrEnum):
    # rules
    MISSING_FIELD = "missing_field"
    DEVICE_IMEI_MISSING = "device_imei_missing"
    DEVICE_IMEI_MALFORMED = "device_imei_malformed"
    DEVICE_ID_MISSING = "device_id_missing"
    ENTITY_NAME_DIFFERS = "entity_name_differs"
    SHIPMENT_DEVICE_MISMATCH = "shipment_device_mismatch"
    TIMESTAMP_IMPLAUSIBLE = "timestamp_implausible"
    TIMESTAMP_MISMATCH = "timestamp_mismatch"
    LATITUDE_OUT_OF_RANGE = "latitude_out_of_range"
    LONGITUDE_OUT_OF_RANGE = "longitude_out_of_range"
    TEMPERATURE_FAULT = "temperature_fault"
    TEMPERATURE_NULL = "temperature_null"
    PROBE_FAULT = "probe_fault"
    HUMIDITY_OUT_OF_RANGE = "humidity_out_of_range"
    LUX_OUT_OF_RANGE = "lux_out_of_range"
    BATTERY_OUT_OF_RANGE = "battery_out_of_range"
    DBM_OUT_OF_RANGE = "dbm_out_of_range"
    ACCURACY_OUT_OF_RANGE = "accuracy_out_of_range"
    MAGNITUDE_COMPUTED = "magnitude_computed"
    # parser
    TYPE_ERROR = "type_error"
    UNPARSEABLE_NUMBER = "unparseable_number"
    NOT_AN_INTEGER = "not_an_integer"
    NUMBER_FROM_STRING = "number_from_string"
    UNKNOWN_LOCATION_METHOD = "unknown_location_method"
    # pipeline / persistence / operator
    OUTPUT_SCHEMA_VIOLATION = "output_schema_violation"
    NOT_AN_OBJECT = "not_an_object"
    MALFORMED_JSON = "malformed_json"
    CONFLICTS_EXISTING = "conflicts_existing"
    OPERATOR_OVERRIDE = "operator_override"
    TEMPERATURE_NULLED_BY_OPERATOR = "temperature_nulled_by_operator"


@dataclass(frozen=True)
class Issue:
    code: str
    field: str
    severity: Severity
    message: str
    original: Any = None
    corrected: Any = None
    source: str = "pipeline"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "field": self.field,
            "severity": str(self.severity),
            "message": self.message,
            "original": self.original,
            "corrected": self.corrected,
            "source": self.source,
        }
