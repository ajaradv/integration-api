from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import TestKey
from tests.integration.helpers import submit, variant, view


async def promote(
    client: httpx.AsyncClient, key: TestKey, id_: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.post(f"/api/ingestions/{id_}/promote", json=body, headers=key.headers)


async def scalar(db: AsyncEngine, sql: str, **params: Any) -> Any:
    async with db.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).scalar_one()


async def test_cuj4_promote_sample_2_replaces_sample_1(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
    db: AsyncEngine,
) -> None:
    s1 = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    s2 = (await submit(client, sender_key, samples[1]))["ingestion_id"]
    await drain()
    v2 = await view(client, sender_key, s2)
    assert v2["status"] == "quarantined"
    assert v2["readings"] == []
    assert v2["candidate_sensor"]["device_imei"] == "863257063350583"

    body = {"overrides": {"latitude": 40.81}, "note": "typo in latitude"}
    r = await promote(client, operator_key, s2, body)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["owner_ingestion_id"] == s1

    r = await promote(client, operator_key, s2, {**body, "replace_existing": True})
    assert r.status_code == 200, r.text
    promoted = r.json()
    assert promoted["status"] == "promoted"
    assert promoted["promoted_at"]
    assert promoted["write_result"]["result"] == "replaced"
    assert all(x["current"] for x in promoted["readings"])
    assert promoted["readings"][1]["px_payload"]["latitude"] == 40.81
    assert any(i["source"] == "operator" for i in promoted["issues"])
    assert promoted["issues_history"][0]["issues"][0]["code"] == "latitude_out_of_range"
    stored = await scalar(db, "SELECT promotion_overrides FROM ingestions WHERE id = :i", i=s2)
    assert stored["note"] == "typo in latitude" and stored["overrides"] == {"latitude": 40.81}
    assert str(await scalar(db, "SELECT promoted_by FROM ingestions WHERE id = :i", i=s2)) == str(
        operator_key.id
    )
    assert await scalar(db, "SELECT count(*) FROM readings") == 1
    assert await scalar(db, "SELECT count(*) FROM readings_history") == 1

    v1 = await view(client, sender_key, s1)
    assert v1["status"] == "accepted"
    assert all(not x["current"] and x["superseded_by"] == s2 for x in v1["readings"])


async def test_promote_non_quarantined_is_409(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    s1 = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    r = await promote(client, operator_key, s1, {"note": "x"})
    assert r.status_code == 409  # still pending
    await drain()
    r = await promote(client, operator_key, s1, {"note": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_quarantined"
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await promote(client, operator_key, missing, {"note": "x"})).status_code == 404


async def test_overrides_that_still_fail_return_422_and_change_nothing(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    s2 = (await submit(client, sender_key, samples[1]))["ingestion_id"]
    await drain()
    r = await promote(client, operator_key, s2, {"overrides": {"latitude": 96.0}, "note": "x"})
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["error"]["details"]["issues"]]
    assert "latitude_out_of_range" in codes
    assert (await view(client, sender_key, s2))["status"] == "quarantined"
    r = await promote(client, operator_key, s2, {"overrides": {"device_imei": "12"}, "note": "x"})
    assert r.status_code == 422  # pydantic validation of the override itself
    r = await promote(client, operator_key, s2, {"overrides": {}, "note": ""})
    assert r.status_code == 422


async def test_sample_5_with_unused_imei_is_promoted_and_with_used_imei_conflicts(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    await submit(client, sender_key, samples[0])
    s5 = (await submit(client, sender_key, samples[4]))["ingestion_id"]
    await drain()
    assert (await view(client, sender_key, s5))["status"] == "quarantined"
    used = await promote(
        client, operator_key, s5, {"overrides": {"device_imei": "863257063350583"}, "note": "n"}
    )
    assert used.status_code == 409
    ok = await promote(
        client, operator_key, s5, {"overrides": {"device_imei": "866088073468499"}, "note": "n"}
    )
    assert ok.status_code == 200 and ok.json()["status"] == "promoted"
    assert ok.json()["write_result"]["result"] == "inserted"
    assert ok.json()["readings"][0]["px_payload"]["device_imei"] == "866088073468499"


async def test_temperature_fault_promoted_with_null_temperature(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    s = (await submit(client, sender_key, variant(samples[2], 327.67)))["ingestion_id"]
    await drain()
    assert (await view(client, sender_key, s))["status"] == "quarantined"
    r = await promote(
        client, operator_key, s, {"overrides": {"temperature": None}, "note": "sensor fault"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "promoted"
    assert body["readings"][0]["px_payload"]["temperature"] is None
    codes = [i["code"] for i in body["issues"]]
    assert "temperature_nulled_by_operator" in codes and "temperature_null" in codes


async def test_conflicts_existing_can_be_promoted_with_replace_existing(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
    db: AsyncEngine,
) -> None:
    a = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    await drain()
    b = (await submit(client, sender_key, variant(samples[0], 11.0)))["ingestion_id"]
    async with db.begin() as conn:
        await conn.execute(
            sa.text("UPDATE ingestions SET reprocess_count = 1 WHERE id = :i"), {"i": b}
        )
    await drain()
    assert (await view(client, sender_key, b))["status"] == "quarantined"
    assert (await promote(client, operator_key, b, {"note": "n"})).status_code == 409
    r = await promote(client, operator_key, b, {"note": "n", "replace_existing": True})
    assert r.status_code == 200 and r.json()["write_result"]["result"] == "replaced"
    assert (await view(client, sender_key, a))["readings"][0]["superseded_by"] == b


async def test_reprocess_moves_issues_to_history_and_resets_attempts(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
    db: AsyncEngine,
) -> None:
    s2 = (await submit(client, sender_key, samples[1]))["ingestion_id"]
    await drain()
    r = await client.post(f"/api/ingestions/{s2}/reprocess", headers=operator_key.headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    v = await view(client, sender_key, s2)
    assert v["status"] == "pending" and v["attempts"] == 0 and v["reprocess_count"] == 1
    assert v["issues"] == [] and v["candidate_sensor"] is None
    assert v["issues_history"][0]["issues"][0]["code"] == "latitude_out_of_range"
    assert str(await scalar(db, "SELECT reprocessed_by FROM ingestions WHERE id=:i", i=s2)) == str(
        operator_key.id
    )
    # pending is not reprocessable
    again = await client.post(f"/api/ingestions/{s2}/reprocess", headers=operator_key.headers)
    assert again.status_code == 409
    await drain()
    assert (await view(client, sender_key, s2))["status"] == "quarantined"
    missing = "00000000-0000-0000-0000-000000000000"
    r = await client.post(f"/api/ingestions/{missing}/reprocess", headers=operator_key.headers)
    assert r.status_code == 404


async def test_bulk_reprocess_returns_count(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    for i in (1, 4, 5, 8):  # samples 2, 5, 6, 9 are quarantined
        await submit(client, sender_key, samples[i])
    await submit(client, sender_key, samples[0])
    await drain()
    r = await client.post(
        "/api/ingestions/reprocess?status=quarantined&limit=3", headers=operator_key.headers
    )
    assert r.status_code == 200 and r.json() == {"queued": 3}
    r = await client.post(
        "/api/ingestions/reprocess?status=quarantined", headers=operator_key.headers
    )
    assert r.json() == {"queued": 1}
    r = await client.post("/api/ingestions/reprocess?status=accepted", headers=operator_key.headers)
    assert r.status_code == 422
    r = await client.post("/api/ingestions/reprocess?status=failed", headers=operator_key.headers)
    assert r.json() == {"queued": 0}


async def test_sender_key_gets_403_on_operator_endpoints(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]]
) -> None:
    s = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    h = sender_key.headers
    assert (await client.get("/api/ingestions", headers=h)).status_code == 403
    assert (
        await client.post(f"/api/ingestions/{s}/promote", json={"note": "x"}, headers=h)
    ).status_code == 403
    assert (await client.post(f"/api/ingestions/{s}/reprocess", headers=h)).status_code == 403
    assert (
        await client.post("/api/ingestions/reprocess?status=failed", headers=h)
    ).status_code == 403
    assert (await client.get(f"/api/ingestions/{s}", headers=h)).status_code == 200


async def test_listing_filters_and_paginates(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    ids = [(await submit(client, sender_key, s))["ingestion_id"] for s in samples]
    await drain()
    r = await client.get("/api/ingestions?status=quarantined", headers=operator_key.headers)
    assert r.status_code == 200
    assert len(r.json()["items"]) == 5 and r.json()["next_cursor"] is None

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        url = "/api/ingestions?limit=5" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url, headers=operator_key.headers)).json()
        seen += [i["id"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 3 and len(seen) == len(set(seen)) == 13
    assert seen == list(reversed(ids))  # newest first

    r = await client.get(
        "/api/ingestions?device_imei=863257063350583", headers=operator_key.headers
    )
    assert {i["status"] for i in r.json()["items"]} >= {"accepted", "quarantined"}
    assert (
        await client.get("/api/ingestions?status=bogus", headers=operator_key.headers)
    ).status_code == 422
    assert (
        await client.get("/api/ingestions?cursor=zzz", headers=operator_key.headers)
    ).status_code == 422
    assert (
        await client.get("/api/ingestions?limit=500", headers=operator_key.headers)
    ).status_code == 422


async def test_batch_scopes_sender_to_own_ingestions(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    operator_key: TestKey,
    make_key: Any,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
) -> None:
    other = await make_key("sender", "other")
    mine = (await submit(client, sender_key, samples[0]))["ingestion_id"]
    theirs = (await submit(client, other, samples[2]))["ingestion_id"]
    await drain()
    r = await client.get(f"/api/ingestions/batch?ids={mine},{theirs}", headers=sender_key.headers)
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [mine]
    assert r.json()["items"][0]["readings"][0]["kind"] == "sensor"
    r = await client.get(
        f"/api/ingestions/batch?ids={mine}&ids={theirs}", headers=operator_key.headers
    )
    assert [i["id"] for i in r.json()["items"]] == [mine, theirs]
    too_many = ",".join(["00000000-0000-0000-0000-000000000000"] * 101)
    assert (
        await client.get(f"/api/ingestions/batch?ids={too_many}", headers=sender_key.headers)
    ).status_code == 422
    assert (
        await client.get("/api/ingestions/batch?ids=nope", headers=sender_key.headers)
    ).status_code == 422
    r = await client.get("/api/ingestions/batch?ids=", headers=sender_key.headers)
    assert r.status_code == 200 and r.json()["items"] == []
