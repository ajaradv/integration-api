from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.ingest import repository as repo
from app.transform import transform
from tests.conftest import NOW_MS, TestKey
from tests.integration.helpers import submit, variant, view


async def scalar(db: AsyncEngine, sql: str, **params: Any) -> Any:
    async with db.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar_one()


async def upsert(db: AsyncEngine, payload: dict[str, Any], ingestion_id: str, foreign: bool) -> Any:
    result = transform(payload, None, NOW_MS)
    assert result.px_sensor and result.px_location and result.extras
    async with db.begin() as conn:
        return await repo.upsert_reading(
            conn,
            result.px_sensor,
            result.px_location,
            result.extras,
            UUID(str(ingestion_id)),
            foreign,
        )


async def test_concurrent_different_payloads_leave_one_row_and_one_history(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    a = variant(samples[0], 11.0)
    b = variant(samples[0], 12.0)
    ia = (await submit(client, sender_key, a))["ingestion_id"]
    ib = (await submit(client, sender_key, b))["ingestion_id"]
    out = await asyncio.gather(upsert(db, a, ia, True), upsert(db, b, ib, True))
    assert sorted(o.result for o in out) == ["inserted", "replaced"]
    assert await scalar(db, "SELECT count(*) FROM readings") == 1
    assert await scalar(db, "SELECT count(*) FROM readings_history") == 1


async def test_unchanged_keeps_ingestion_id_and_replaced_updates_it_with_history(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    a = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    b = (await submit(client, sender_key, variant(samples[0], 11.0)))["ingestion_id"]
    assert (await upsert(db, samples[0], a, True)).result == "inserted"
    again = await upsert(db, samples[0], b, True)
    assert again.result == "unchanged" and str(again.owner_ingestion_id) == a
    assert str(await scalar(db, "SELECT ingestion_id FROM readings")) == a
    assert await scalar(db, "SELECT count(*) FROM readings_history") == 0

    replaced = await upsert(db, variant(samples[0], 11.0), b, True)
    assert replaced.result == "replaced"
    assert str(await scalar(db, "SELECT ingestion_id FROM readings")) == b
    assert float(await scalar(db, "SELECT temperature FROM readings")) == 11.0
    assert str(await scalar(db, "SELECT ingestion_id FROM readings_history")) == a
    assert str(await scalar(db, "SELECT replaced_by_ingestion_id FROM readings_history")) == b
    assert float(await scalar(db, "SELECT temperature FROM readings_history")) == 10.08


async def test_foreign_reading_is_not_replaced_when_not_allowed(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    a = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    b = (await submit(client, sender_key, variant(samples[0], 11.0)))["ingestion_id"]
    await upsert(db, samples[0], a, True)
    out = await upsert(db, variant(samples[0], 11.0), b, False)
    assert out.result == "conflict" and str(out.owner_ingestion_id) == a
    assert float(await scalar(db, "SELECT temperature FROM readings")) == 10.08
    assert await scalar(db, "SELECT count(*) FROM readings_history") == 0


async def test_reprocessed_ingestion_meeting_a_foreign_reading_is_quarantined(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    drain: Callable[[], Awaitable[int]],
) -> None:
    a = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    await drain()
    b = (await submit(client, sender_key, variant(samples[0], 11.0)))["ingestion_id"]
    # B is a reprocess (reprocess_count > 0), so it may not replace A's reading
    async with db.begin() as conn:
        await conn.execute(
            sa.text("UPDATE ingestions SET reprocess_count = 1 WHERE id = :i"), {"i": b}
        )
    await drain()
    v = await view(client, sender_key, b)
    assert v["status"] == "quarantined"
    assert [i["code"] for i in v["issues"] if i["severity"] == "blocking"] == ["conflicts_existing"]
    assert a in v["issues"][-1]["message"]
    assert v["candidate_sensor"]["temperature"] == 11.0
    assert v["readings"] == [] and v["write_result"] is None
    assert float(await scalar(db, "SELECT temperature FROM readings")) == 10.08
    assert str(await scalar(db, "SELECT ingestion_id FROM readings")) == a


@pytest.mark.parametrize(
    ("sensor_patch", "location_patch"),
    [
        ({"device_imei": "123"}, {"device_imei": "123"}),
        ({}, {"latitude": 95.0}),
        ({}, {"longitude": -200.0}),
        ({"timestamp": 0}, {"timestamp": 0}),
        ({"device_imei": None}, {"device_imei": None}),
        ({"device_imei": "863257063350583"}, {"device_imei": "866088073468439"}),
        ({"timestamp": 5}, {"timestamp": 6}),
    ],
)
async def test_check_constraints_reject_invalid_rows(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    sensor_patch: dict[str, Any],
    location_patch: dict[str, Any],
) -> None:
    ing = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    ok = transform(samples[0], None, NOW_MS)
    assert ok.px_sensor and ok.px_location
    sensor = {**ok.px_sensor, **sensor_patch}
    location = {**ok.px_location, **location_patch}
    with pytest.raises(IntegrityError):
        async with db.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO readings (ingestion_id, px_sensor, px_location) "
                    "VALUES (:i, CAST(:s AS jsonb), CAST(:l AS jsonb))"
                ),
                {"i": ing, "s": repo.dumps(sensor), "l": repo.dumps(location)},
            )
    assert await scalar(db, "SELECT count(*) FROM readings") == 0


async def test_status_view_shows_current_and_superseded(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    a = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    await drain()
    va = await view(client, sender_key, a)
    assert [(r["kind"], r["current"], r["superseded_by"]) for r in va["readings"]] == [
        ("sensor", True, None),
        ("location", True, None),
    ]
    assert va["probe_temperature"] == 9.94 and va["probe_fault"] is False

    # an identical-content ingestion (same device and timestamp, different formatting of
    # an ignored field) is "unchanged" and still shows the records
    twin = {**samples[0], "PublicShipmentId": "other"}
    t = (await submit(client, sender_key, twin))["ingestion_id"]
    await drain()
    vt = await view(client, sender_key, t)
    assert vt["write_result"]["result"] == "unchanged"
    assert all(r["current"] for r in vt["readings"])

    b = (await submit(client, sender_key, variant(samples[0], 11.0)))["ingestion_id"]
    await drain()
    vb = await view(client, sender_key, b)
    assert vb["write_result"]["result"] == "replaced"
    assert all(r["current"] and r["superseded_by"] is None for r in vb["readings"])
    va = await view(client, sender_key, a)
    assert all(not r["current"] and r["superseded_by"] == b for r in va["readings"])
    assert va["readings"][0]["px_payload"]["temperature"] == 10.08  # the superseded version
    vt = await view(client, sender_key, t)
    assert all(not r["current"] and r["superseded_by"] == b for r in vt["readings"])


async def test_settings_fixture_defaults(settings: Settings) -> None:
    assert settings.max_attempts == 5
