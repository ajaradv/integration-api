"""R13 end to end: worker check, backfill, batch ordering, promotion with acknowledge."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.worker import loop as worker_loop
from tests.conftest import TestKey
from tests.integration.helpers import submit, view

HOUR_MS = 3_600_000


async def promote(
    client: httpx.AsyncClient, key: TestKey, id_: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.post(f"/api/ingestions/{id_}/promote", json=body, headers=key.headers)


async def scalar(db: AsyncEngine, sql: str, **params: Any) -> Any:
    async with db.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar_one()


def blocking(v: dict[str, Any]) -> list[str]:
    return [i["code"] for i in v["issues"] if i["severity"] == "blocking"]


def at(sample: dict[str, Any], ts: int, lat: float | None = None) -> dict[str, Any]:
    out = copy.deepcopy(sample)
    out["EntryTimeEpoch"] = ts
    out.pop("EntryTimeUtc", None)
    if lat is not None:
        out["Location"]["Latitude"] = lat
    return out


async def test_jump_is_quarantined_then_promoted_with_acknowledge(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
    db: AsyncEngine,
) -> None:
    sf = (await submit(client, sender_key, samples[2]))["ingestion_id"]
    richmond = (await submit(client, sender_key, samples[3]))["ingestion_id"]
    await drain()

    assert (await view(client, sender_key, sf))["status"] == "accepted"
    v = await view(client, sender_key, richmond)
    assert v["status"] == "quarantined"
    assert blocking(v) == ["location_jump"]
    assert v["candidate_location"]["latitude"] == 37.5407
    assert await scalar(db, "SELECT count(*) FROM readings") == 1

    r = await promote(client, operator_key, richmond, {"note": "confirmed air freight"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "promotion_blocked"

    r = await promote(
        client,
        operator_key,
        richmond,
        {"note": "confirmed air freight", "acknowledge": ["location_jump"]},
    )
    assert r.status_code == 200, r.text
    promoted = r.json()
    assert promoted["status"] == "promoted"
    assert promoted["write_result"]["result"] == "inserted"
    jump = next(i for i in promoted["issues"] if i["code"] == "location_jump")
    assert (jump["severity"], jump["source"]) == ("warning", "operator")
    assert promoted["issues_history"][-1]["issues"][-1]["code"] == "location_jump"
    stored = await scalar(
        db, "SELECT promotion_overrides FROM ingestions WHERE id = :i", i=richmond
    )
    assert stored["acknowledge"] == ["location_jump"]
    assert await scalar(db, "SELECT count(*) FROM readings") == 2

    # the promoted reading is now the baseline: a later Richmond fix passes
    later = at(samples[3], samples[3]["EntryTimeEpoch"] + HOUR_MS)
    nxt = (await submit(client, sender_key, later))["ingestion_id"]
    await drain()
    assert (await view(client, sender_key, nxt))["status"] == "accepted"


async def test_backfill_is_checked_against_the_following_reading(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    richmond = (await submit(client, sender_key, samples[3]))["ingestion_id"]
    await drain()
    sf = (await submit(client, sender_key, samples[2]))["ingestion_id"]  # older, arrives later
    await drain()

    assert (await view(client, sender_key, richmond))["status"] == "accepted"
    v = await view(client, sender_key, sf)
    assert blocking(v) == ["location_jump"]
    jump = next(i for i in v["issues"] if i["code"] == "location_jump")
    assert jump["original"]["neighbour"] == "next"
    assert jump["original"]["ingestion_id"] == richmond


async def test_overrides_can_resolve_a_jump(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    await submit(client, sender_key, samples[2])
    richmond = (await submit(client, sender_key, samples[3]))["ingestion_id"]
    await drain()
    body = {"overrides": {"latitude": 37.7749, "longitude": -122.4194}, "note": "bad fix"}
    r = await promote(client, operator_key, richmond, body)
    assert r.status_code == 200, r.text
    assert "location_jump" not in [i["code"] for i in r.json()["issues"]]


async def test_acknowledge_rejects_unknown_codes(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    s2 = (await submit(client, sender_key, samples[1]))["ingestion_id"]
    await drain()
    body = {"note": "x", "acknowledge": ["latitude_out_of_range"]}
    r = await promote(client, operator_key, s2, body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_plausible_sequence_is_accepted(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    # samples 1 and 8: same device, 45 m apart in 15 minutes
    ids = [(await submit(client, sender_key, s))["ingestion_id"] for s in (samples[0], samples[7])]
    await drain()
    for i in ids:
        assert "location_jump" not in [
            x["code"] for x in (await view(client, sender_key, i))["issues"]
        ]


async def test_concurrent_batch_checks_in_arrival_order(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    settings: Settings,
) -> None:
    """run_once claims both readings in one batch; the later-received one is flagged."""
    base = samples[2]["EntryTimeEpoch"]
    payloads = [
        at(samples[2], base),
        at(samples[2], base + HOUR_MS, lat=10.0),  # ~3,100 km south in 1 h
        at(samples[10], samples[10]["EntryTimeEpoch"]),  # another device, runs concurrently
    ]
    ids = [(await submit(client, sender_key, p))["ingestion_id"] for p in payloads]
    assert await worker_loop.run_once(db, settings) == 3
    statuses = [(await view(client, sender_key, i))["status"] for i in ids]
    assert statuses == ["accepted", "quarantined", "accepted"]


def test_device_lanes_group_by_device_in_order() -> None:
    rows = [
        {"id": 1, "raw_json": {"DeviceId": "a"}},
        {"id": 2, "raw_json": {"DeviceId": "b"}},
        {"id": 3, "raw_json": {"DeviceId": "a"}},
        {"id": 4, "raw_json": None},
        {"id": 5, "raw_json": {"DeviceId": None}},
    ]
    lanes = [[r["id"] for r in lane] for lane in worker_loop.device_lanes(rows)]
    assert lanes == [[1, 3], [2], [4], [5]]
