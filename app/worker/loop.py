"""The worker: claim, transform, persist, heartbeat, reclaim."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.ingest import repository as repo
from app.ingest.hashing import MalformedBody, strict_parse
from app.transform import PipelineResult, transform
from app.transform.issues import Issue, IssueCode, Severity

log = structlog.get_logger("worker")

RECLAIM_EVERY_S = 60.0


def worker_id() -> str:
    import os

    return f"{socket.gethostname()}-{os.getpid()}"


async def _malformed_result(engine: AsyncEngine, id_: UUID) -> PipelineResult:
    """A row with SQL-NULL raw_json had a malformed body; re-check the stored bytes."""
    async with engine.connect() as conn:
        body = (
            await conn.execute(
                sa.text("SELECT raw_body FROM ingestions WHERE id = :id"), {"id": id_}
            )
        ).scalar_one()
    parsed = strict_parse(bytes(body))
    reason = parsed.reason if isinstance(parsed, MalformedBody) else "body is not valid JSON"
    return PipelineResult(
        "rejected",
        [Issue(IssueCode.MALFORMED_JSON, "$", Severity.BLOCKING, reason)],
        None,
        None,
        None,
    )


async def process_one(
    engine: AsyncEngine, settings: Settings, row: dict[str, Any], now_ms: int | None = None
) -> str | None:
    """Transform and persist one claimed row. Returns the final status, or None if released.

    On an exception, a timeout or cancellation the row goes back to `pending` with backoff,
    or to `failed` once its attempts are exhausted.
    """
    id_: UUID = row["id"]
    attempts: int = row["attempts"]
    bound = structlog.contextvars.bound_contextvars(ingestion_id=str(id_))
    try:
        with bound:
            now = now_ms if now_ms is not None else int(time.time() * 1000)
            if row.get("malformed"):
                result = await _malformed_result(engine, id_)
            else:
                result = transform(row["raw_json"], None, now)
            async with engine.begin() as conn:
                status = await repo.persist_result(
                    conn,
                    ingestion_id=id_,
                    result=result,
                    allow_replace_foreign=row["reprocess_count"] == 0,
                )
            log.info(
                "ingestion_processed",
                status=status,
                issue_codes=[str(i.code) for i in result.issues],
            )
            if any(i.code == IssueCode.OUTPUT_SCHEMA_VIOLATION for i in result.issues):
                log.error("output_schema_violation", issues=[i.to_dict() for i in result.issues])
            return status
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        cancelled = isinstance(exc, asyncio.CancelledError)
        log.error("ingestion_failed", ingestion_id=str(id_), attempts=attempts, error=error)
        try:
            # shield: the release must complete even while this task is being cancelled
            await asyncio.shield(_release(engine, settings, id_, attempts, error))
        except Exception:  # pragma: no cover
            log.error("release_failed", ingestion_id=str(id_))
        if cancelled:
            raise
        return None


async def _release(
    engine: AsyncEngine, settings: Settings, id_: UUID, attempts: int, error: str
) -> None:
    async with engine.begin() as conn:
        if attempts >= settings.max_attempts:
            await repo.mark_failed(conn, id_, error)
        else:
            await repo.release_for_retry(conn, id_, error, attempts)


async def _run_job(engine: AsyncEngine, settings: Settings, row: dict[str, Any]) -> str | None:
    try:
        return await asyncio.wait_for(process_one(engine, settings, row), settings.job_timeout_s)
    except TimeoutError:
        # wait_for cancelled process_one, whose handler already released the row
        return None


async def beat_once(engine: AsyncEngine, wid: str) -> None:
    async with engine.begin() as conn:
        await repo.beat(conn, wid)


async def reclaim_once(engine: AsyncEngine, settings: Settings) -> tuple[int, int]:
    async with engine.begin() as conn:
        return await repo.reclaim_stale(conn, settings.claim_stale_s, settings.max_attempts)


async def run_once(engine: AsyncEngine, settings: Settings) -> int:
    """Claim one batch and process it concurrently. Returns the number of rows claimed."""
    async with engine.begin() as conn:
        rows = await repo.claim_batch(conn, settings.worker_max_jobs, settings.max_attempts)
    if rows:
        await asyncio.gather(*(_run_job(engine, settings, r) for r in rows))
    return len(rows)


async def run(settings: Settings, engine: AsyncEngine, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    wid = worker_id()
    await beat_once(engine, wid)  # before the first claim
    released, failed = await reclaim_once(engine, settings)
    log.info("worker_started", worker_id=wid, reclaimed=released, failed=failed)
    last_beat = last_reclaim = time.monotonic()
    sleep_s = settings.worker_poll_min_s
    while not stop.is_set():
        try:
            found = await run_once(engine, settings)
            now = time.monotonic()
            if now - last_beat >= settings.heartbeat_interval_s:
                await beat_once(engine, wid)
                last_beat = now
            if now - last_reclaim >= RECLAIM_EVERY_S:
                await reclaim_once(engine, settings)
                last_reclaim = now
        except Exception as exc:  # a database blip must not kill the worker
            log.error("worker_loop_error", error=f"{type(exc).__name__}: {exc}")
            found = 0
        sleep_s = (
            settings.worker_poll_min_s if found else min(sleep_s * 2, settings.worker_poll_max_s)
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=sleep_s)
    log.info("worker_stopped", worker_id=wid)
