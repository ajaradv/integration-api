from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import TestKey

URL = "/api/webhook/tive"


async def count(db: AsyncEngine, where: str = "TRUE") -> int:
    async with db.connect() as conn:
        return (
            await conn.execute(sa.text(f"SELECT count(*) FROM ingestions WHERE {where}"))
        ).scalar_one()


async def test_valid_request_returns_202_and_creates_pending_row(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    r = await client.post(URL, json=samples[0], headers=sender_key.headers)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    assert body["duplicate"] is False
    assert body["status_url"] == f"/api/ingestions/{body['ingestion_id']}"
    assert await count(db, "status = 'pending'") == 1
    assert r.headers["x-request-id"]


async def test_same_body_again_returns_200_with_same_id(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    first = await client.post(URL, json=samples[0], headers=sender_key.headers)
    # same content, different key order and whitespace
    reordered = json.dumps(dict(reversed(list(samples[0].items()))), indent=3).encode()
    second = await client.post(URL, content=reordered, headers=sender_key.headers)
    assert second.status_code == 200
    assert second.json()["ingestion_id"] == first.json()["ingestion_id"]
    assert second.json()["duplicate"] is True
    assert await count(db) == 1


async def test_same_body_under_different_key_gets_new_id(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    make_key: Any,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
) -> None:
    other = await make_key("sender", "other")
    a = await client.post(URL, json=samples[0], headers=sender_key.headers)
    b = await client.post(URL, json=samples[0], headers=other.headers)
    assert b.status_code == 202
    assert a.json()["ingestion_id"] != b.json()["ingestion_id"]


async def test_idempotency_header_same_body_returns_200(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]]
) -> None:
    h = {**sender_key.headers, "Idempotency-Key": "delivery-1"}
    a = await client.post(URL, json=samples[0], headers=h)
    b = await client.post(URL, json=samples[0], headers=h)
    assert (a.status_code, b.status_code) == (202, 200)
    assert a.json()["ingestion_id"] == b.json()["ingestion_id"]


async def test_idempotency_key_reused_with_different_body_is_409_and_not_stored(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    h = {**sender_key.headers, "Idempotency-Key": "delivery-1"}
    a = await client.post(URL, json=samples[0], headers=h)
    b = await client.post(URL, json=samples[2], headers=h)
    assert (a.status_code, b.status_code) == (202, 409)
    assert b.json()["error"]["code"] == "idempotency_key_reused"
    assert await count(db) == 1


async def test_identical_bodies_with_different_headers_create_two_rows(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    a = await client.post(
        URL, json=samples[0], headers={**sender_key.headers, "Idempotency-Key": "a"}
    )
    b = await client.post(
        URL, json=samples[0], headers={**sender_key.headers, "Idempotency-Key": "b"}
    )
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["ingestion_id"] != b.json()["ingestion_id"]
    assert await count(db) == 2


async def test_race_same_key_different_bodies_gives_one_202_and_one_409(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    h = {**sender_key.headers, "Idempotency-Key": "race"}
    results = await asyncio.gather(
        client.post(URL, json=samples[0], headers=h),
        client.post(URL, json=samples[2], headers=h),
    )
    assert sorted(r.status_code for r in results) == [202, 409]
    assert await count(db) == 1


async def test_race_same_body_no_header_gives_one_202_and_one_200(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    results = await asyncio.gather(
        *[client.post(URL, json=samples[0], headers=sender_key.headers) for _ in range(4)]
    )
    assert sorted(r.status_code for r in results) == [200, 200, 200, 202]
    assert len({r.json()["ingestion_id"] for r in results}) == 1
    assert await count(db) == 1


async def test_oversized_body_without_key_is_413_not_401(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    r = await client.post(URL, content=b"x" * 300_000)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    assert await count(db) == 0


async def test_oversized_streamed_body_is_413(
    client: httpx.AsyncClient, sender_key: TestKey, db: AsyncEngine
) -> None:
    async def chunks() -> Any:
        for _ in range(40):
            yield b"x" * 10_000

    r = await client.post(URL, content=chunks(), headers=sender_key.headers)
    assert r.status_code == 413
    assert await count(db) == 0


async def test_no_key_is_401(
    client: httpx.AsyncClient, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    r = await client.post(URL, json=samples[0])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert await count(db) == 0


async def test_unknown_and_revoked_key_are_401(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    r = await client.post(URL, json=samples[0], headers={"X-API-Key": "pxk_nope"})
    assert r.status_code == 401
    async with db.begin() as conn:
        await conn.execute(sa.text("UPDATE api_keys SET revoked_at = now()"))
    r = await client.post(URL, json=samples[0], headers=sender_key.headers)
    assert r.status_code == 401
    assert await count(db) == 0


async def test_operator_key_on_webhook_is_403(
    client: httpx.AsyncClient, operator_key: TestKey, samples: list[dict[str, Any]]
) -> None:
    r = await client.post(URL, json=samples[0], headers=operator_key.headers)
    assert r.status_code == 403


async def test_malformed_idempotency_key_is_400_and_not_stored(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    for bad in ["", "x" * 256, "tab\there", "café"]:
        h = {**sender_key.headers, "Idempotency-Key": bad}
        try:
            r = await client.post(URL, json=samples[0], headers=h)
        except (UnicodeEncodeError, httpx.InvalidURL):
            continue
        assert r.status_code == 400, bad
        assert r.json()["error"]["code"] == "invalid_idempotency_key"
    assert await count(db) == 0


async def test_malformed_json_is_400_and_stored_as_rejected(
    client: httpx.AsyncClient, sender_key: TestKey, db: AsyncEngine
) -> None:
    for n, body in enumerate(
        [b"not json", b'{"a": NaN}', b'{"a": "x\\u0000"}', b"\xff\xfe\x00", b'{"a": 1']
    ):
        r = await client.post(URL, content=body, headers=sender_key.headers)
        assert r.status_code == 400, body
        assert r.json()["ingestion_id"]
        assert r.json()["error"]["code"] == "malformed_json"
        assert await count(db, "status = 'rejected'") == n + 1
    async with db.connect() as conn:
        raw = (
            await conn.execute(
                sa.text("SELECT raw_body FROM ingestions WHERE raw_body = :b"),
                {"b": b"\xff\xfe\x00"},
            )
        ).scalar_one()
    assert bytes(raw) == b"\xff\xfe\x00"


async def test_resend_of_malformed_body_returns_400_with_original_id(
    client: httpx.AsyncClient, sender_key: TestKey, db: AsyncEngine
) -> None:
    a = await client.post(URL, content=b"nope", headers=sender_key.headers)
    b = await client.post(URL, content=b"nope", headers=sender_key.headers)
    assert (a.status_code, b.status_code) == (400, 400)
    assert a.json()["ingestion_id"] == b.json()["ingestion_id"]
    assert await count(db) == 1


async def test_valid_json_non_object_is_accepted_for_the_worker_to_reject(
    client: httpx.AsyncClient, sender_key: TestKey, drain: Any, db: AsyncEngine
) -> None:
    r = await client.post(URL, content=b"[1, 2]", headers=sender_key.headers)
    assert r.status_code == 202
    await drain()
    v = (
        await client.get(f"/api/ingestions/{r.json()['ingestion_id']}", headers=sender_key.headers)
    ).json()
    assert v["status"] == "rejected"
    assert v["issues"][0]["code"] == "not_an_object"


async def test_error_envelope_and_docs(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/ingestions/not-a-uuid", headers={"X-API-Key": "x"})
    assert r.status_code in (401, 422)
    assert "error" in r.json()
    assert (await client.get("/docs")).status_code == 200
