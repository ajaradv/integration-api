"""Strict body parsing and hashing for the request path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MalformedBody:
    reason: str


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not allowed")


def _parse_float(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError("number out of range")
    return value


def _has_nul(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            for key, child in item.items():
                if "\x00" in key:
                    return True
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return False


def strict_parse(body: bytes) -> Any | MalformedBody:
    """Parse JSON the way Postgres jsonb can store it, or return MalformedBody."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return MalformedBody("body is not valid UTF-8")
    try:
        parsed = json.loads(text, parse_constant=_reject_constant, parse_float=_parse_float)
    except RecursionError:
        return MalformedBody("JSON is nested too deeply")
    except ValueError as exc:
        return MalformedBody(f"body is not valid JSON: {exc}")
    if _has_nul(parsed):
        return MalformedBody("JSON contains a \\u0000 character, which cannot be stored")
    return parsed


def canonical_hash(parsed: Any) -> str:
    text = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def raw_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
