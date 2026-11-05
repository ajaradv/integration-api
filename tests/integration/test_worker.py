from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.ingest import operator
from app.ingest import repository as repo
from app.worker import loop as worker_loop
from tests.conftest import NOW_MS, TestKey
from tests.integration.helpers import submit, view


async def row_of(db: AsyncEngine, id_: str) -> Any:
    async with db.connect() as conn:
        return (
            await conn.execute(sa.text("SELECT * FROM ingestions WHERE id = :i"), {"i": id_})
        ).one()


async def test_claimed_and_processed_row_gets_expected_status(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    run_worker_once: Callable[[], Awaitable[int]],
) -> None:
    sub = await submit(client, sender_key, samples[0])
    assert await run_worker_once() == 1
    v = await view(client, sender_key, sub["ingestion_id"])
    assert v["status"] == "accepted"
    assert v["attempts"] == 1
    assert v["processed_at"]
    assert [r["kind"] for r in v["readings"]] == ["sensor", "location"]
    assert all(r["current"] for r in v["readings"])
    assert v["write_result"]["result"] == "inserted"
    assert await run_worker_once() == 0


async def test_concurrent_claims_never_return_the_same_row(
    client: httpx.AsyncClient, sender_key: TestKey, samples: list[dict[str, Any]], db: AsyncEngine
) -> None:
    for s in samples[:12]:
        await submit(client, sender_key, s)

    async def claim() -> list[Any]:
        async with db.begin() as conn:
            rows = await repo.claim_batch(conn, 5, 5)
            await asyncio.sleep(0.05)  # hold the locks while the other claims
            return [r["id"] for r in rows]

    a, b, c = await asyncio.gather(claim(), claim(), claim())
    ids = a + b + c
    assert len(ids) == len(set(ids)) == 12


async def test_failing_job_retries_with_backoff_then_fails(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: Any) -> Any:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker_loop, "transform", boom)
    sub = await submit(client, sender_key, samples[0])
    for attempt in range(1, settings.max_attempts + 1):
        async with db.begin() as conn:
            rows = await repo.claim_batch(conn, 4, settings.max_attempts)
        assert len(rows) == 1 and rows[0]["attempts"] == attempt
        assert await worker_loop.process_one(db, settings, rows[0], NOW_MS) is None
        row = await row_of(db, sub["ingestion_id"])
        if attempt < settings.max_attempts:
            assert row.status == "pending"
            assert "kaboom" in row.last_error
            # backoff: next attempt is 2^attempt seconds away, so it is not claimable yet
            async with db.begin() as conn:
                assert await repo.claim_batch(conn, 4, settings.max_attempts) == []
                await conn.execute(sa.text("UPDATE ingestions SET next_attempt_at = NULL"))
    assert row.status == "failed"
    assert row.attempts == settings.max_attempts
    async with db.begin() as conn:
        assert await repo.claim_batch(conn, 4, settings.max_attempts) == []


async def test_timeout_releases_the_row(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(*_: Any, **__: Any) -> str:
        await asyncio.sleep(5)
        return "accepted"

    monkeypatch.setattr(worker_loop.repo, "persist_result", slow)
    sub = await submit(client, sender_key, samples[0])
    fast = settings.model_copy(update={"job_timeout_s": 0.2})
    async with db.begin() as conn:
        rows = await repo.claim_batch(conn, 4, fast.max_attempts)
    assert await worker_loop._run_job(db, fast, rows[0]) is None
    row = await row_of(db, sub["ingestion_id"])
    assert row.status == "pending" and row.next_attempt_at is not None
    assert "TimeoutError" in row.last_error or "Cancelled" in row.last_error


async def test_stale_processing_row_is_reclaimed_or_failed(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    settings: Settings,
) -> None:
    a = await submit(client, sender_key, samples[0])
    b = await submit(client, sender_key, samples[2])
    fresh = await submit(client, sender_key, samples[3])
    async with db.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE ingestions SET status='processing', attempts=:n, "
                "claimed_at = now() - interval '10 minutes' WHERE id = :i"
            ),
            [{"n": 2, "i": a["ingestion_id"]}, {"n": 5, "i": b["ingestion_id"]}],
        )
        await conn.execute(
            sa.text(
                "UPDATE ingestions SET status='processing', attempts=1, claimed_at=now() "
                "WHERE id = :i"
            ),
            {"i": fresh["ingestion_id"]},
        )
    assert await worker_loop.reclaim_once(db, settings) == (1, 1)
    assert (await row_of(db, a["ingestion_id"])).status == "pending"
    failed = await row_of(db, b["ingestion_id"])
    assert failed.status == "failed" and failed.last_error == "attempts exhausted"
    assert (await row_of(db, fresh["ingestion_id"])).status == "processing"


async def test_reprocessed_row_is_claimed_again(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    run_worker_once: Callable[[], Awaitable[int]],
) -> None:
    sub = await submit(client, sender_key, samples[1])  # quarantined: latitude 95
    await run_worker_once()
    assert (await row_of(db, sub["ingestion_id"])).status == "quarantined"
    async with db.begin() as conn:
        await operator.reprocess(conn, sub["ingestion_id"], None, source="cli")
    row = await row_of(db, sub["ingestion_id"])
    assert row.status == "pending" and row.attempts == 0 and row.reprocess_count == 1
    assert row.issues is None
    assert row.issues_history[0]["issues"][0]["code"] == "latitude_out_of_range"
    assert row.issues_history[0]["source"] == "cli"
    assert await run_worker_once() == 1
    row = await row_of(db, sub["ingestion_id"])
    assert row.status == "quarantined" and row.attempts == 1  # same payload, same outcome


async def test_heartbeat_is_written(db: AsyncEngine, settings: Settings) -> None:
    await worker_loop.beat_once(db, "w1")
    await worker_loop.beat_once(db, "w1")
    async with db.connect() as conn:
        n = (await conn.execute(sa.text("SELECT count(*) FROM worker_heartbeats"))).scalar_one()
    assert n == 1


async def test_run_loop_processes_and_heartbeats(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    samples: list[dict[str, Any]],
    db: AsyncEngine,
    settings: Settings,
) -> None:
    sub = await submit(client, sender_key, samples[0])
    fast = settings.model_copy(update={"worker_poll_min_s": 0.05, "worker_poll_max_s": 0.1})
    stop = asyncio.Event()
    task = asyncio.create_task(worker_loop.run(fast, db, stop))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await row_of(db, sub["ingestion_id"])).status == "accepted":
            break
    stop.set()
    await task
    assert (await row_of(db, sub["ingestion_id"])).status == "accepted"
    async with db.connect() as conn:
        assert (
            await conn.execute(sa.text("SELECT count(*) FROM worker_heartbeats"))
        ).scalar_one() == 1


async def test_rejected_malformed_row_is_rejected_again_on_reprocess(
    client: httpx.AsyncClient,
    sender_key: TestKey,
    db: AsyncEngine,
    run_worker_once: Callable[[], Awaitable[int]],
) -> None:
    r = await client.post("/api/webhook/tive", content=b"{nope", headers=sender_key.headers)
    id_ = r.json()["ingestion_id"]
    async with db.begin() as conn:
        await operator.reprocess(conn, id_, None, source="cli")
    assert await run_worker_once() == 1
    row = await row_of(db, id_)
    assert row.status == "rejected"
    assert row.issues[0]["code"] == "malformed_json"
