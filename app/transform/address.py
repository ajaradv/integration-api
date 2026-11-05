"""Split a Tive FormattedAddress into PX address parts, only when reliable."""

from __future__ import annotations

import re

_STATE_ZIP = re.compile(r"^([A-Z]{2}) (\d{5}(?:-\d{4})?)$")


def split_address(formatted: str | None) -> dict[str, str | None] | None:
    if formatted is None or not formatted.strip():
        return None
    address: dict[str, str | None] = {
        "street": None,
        "locality": None,
        "state": None,
        "country": None,
        "postal_code": None,
        "full_address": formatted,
    }
    parts = [p.strip() for p in formatted.split(",")]
    if len(parts) >= 3:
        locality, state_zip, country = parts[-3], parts[-2], parts[-1]
        match = _STATE_ZIP.match(state_zip)
        if match and locality and country:
            address["locality"] = locality
            address["state"] = match.group(1)
            address["postal_code"] = match.group(2)
            address["country"] = country
    return address
