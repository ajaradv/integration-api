from __future__ import annotations

import asyncio
import time
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.models import HealthView, LivezView
from app.config import Settings

router = APIRouter(tags=["health"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def _heartbeat_age(engine: AsyncEngine) -> float | None:
    """Seconds since the newest heartbeat; None when there is none. Raises on DB failure."""
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET LOCAL statement_timeout = '1s'"))
        value = (
            await conn.execute(
                sa.text(
                    "SELECT extract(epoch FROM now() - max(beat_at)) AS age FROM worker_heartbeats"
                )
            )
        ).scalar()
    return None if value is None else float(value)


@router.get("/livez", response_model=LivezView)
async def livez(request: Request) -> JSONResponse:
    settings = _settings(request)
    engine: AsyncEngine = request.app.state.engine
    uptime = time.monotonic() - request.app.state.started_monotonic
    if uptime < settings.livez_grace_s:
        return JSONResponse({"status": "ok", "worker": "starting"})
    try:
        age = await asyncio.wait_for(_heartbeat_age(engine), timeout=3)
    except Exception:
        return JSONResponse({"status": "ok", "worker": "unknown"})
    if age is None or age > settings.heartbeat_stale_s:
        return JSONResponse({"status": "unavailable", "worker": "stale"}, status_code=503)
    return JSONResponse({"status": "ok", "worker": "ok"})


@router.get("/health", response_model=HealthView)
async def health(request: Request) -> HealthView:
    engine: AsyncEngine = request.app.state.engine
    try:

        async def query() -> dict[str, Any]:
            async with engine.connect() as conn:
                await conn.execute(sa.text("SET LOCAL statement_timeout = '2s'"))
                row = (
                    await conn.execute(
                        sa.text(
                            "SELECT "
                            "(SELECT extract(epoch FROM now() - max(beat_at)) "
                            " FROM worker_heartbeats) AS age, "
                            "count(*) FILTER (WHERE status = 'pending') AS pending, "
                            "count(*) FILTER (WHERE status = 'processing') AS processing, "
                            "count(*) FILTER (WHERE status = 'failed') AS failed "
                            "FROM ingestions WHERE status IN ('pending','processing','failed')"
                        )
                    )
                ).one()
                return dict(row._mapping)

        data = await asyncio.wait_for(query(), timeout=4)
    except Exception:
        return HealthView(
            database="unreachable",
            worker_heartbeat_age_s=None,
            pending=None,
            processing=None,
            failed=None,
        )
    return HealthView(
        database="ok",
        worker_heartbeat_age_s=None if data["age"] is None else round(float(data["age"]), 1),
        pending=int(data["pending"]),
        processing=int(data["processing"]),
        failed=int(data["failed"]),
    )
