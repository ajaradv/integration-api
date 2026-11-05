"""Stage 4: check the produced PX records against the vendored PX schemas."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft7Validator

from app.transform.issues import Issue, IssueCode, Severity

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_FILES = {"sensor": "px-sensor-schema.json", "location": "px-location-schema.json"}

Kind = Literal["sensor", "location"]


@cache
def _validator(kind: Kind) -> Draft7Validator:
    schema = json.loads((SCHEMA_DIR / _FILES[kind]).read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def validate_px(kind: Kind, record: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for error in sorted(_validator(kind).iter_errors(record), key=lambda e: list(e.absolute_path)):
        pointer = "/" + "/".join(str(p) for p in error.absolute_path)
        issues.append(
            Issue(
                IssueCode.OUTPUT_SCHEMA_VIOLATION,
                f"px_{kind}{pointer if pointer != '/' else ''}",
                Severity.BLOCKING,
                f"{kind} record violates the PX schema at {pointer}: {error.message}",
                original=error.instance if not isinstance(error.instance, dict | list) else None,
            )
        )
    return issues
