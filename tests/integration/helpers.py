from __future__ import annotations

import copy
from typing import Any

import httpx

from tests.conftest import TestKey

URL = "/api/webhook/tive"


async def submit(
    client: httpx.AsyncClient, key: TestKey, payload: Any, **headers: str
) -> dict[str, Any]:
    r = await client.post(URL, json=payload, headers={**key.headers, **headers})
    assert r.status_code in (200, 202), r.text
    return r.json()  # type: ignore[no-any-return]


async def view(client: httpx.AsyncClient, key: TestKey, ingestion_id: str) -> dict[str, Any]:
    r = await client.get(f"/api/ingestions/{ingestion_id}", headers=key.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def variant(sample: dict[str, Any], celsius: float) -> dict[str, Any]:
    """Same device and timestamp, different temperature."""
    out = copy.deepcopy(sample)
    out["Temperature"]["Celsius"] = celsius
    return out
