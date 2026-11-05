from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings
from app.main import create_app


async def set_heartbeat(db: AsyncEngine, age_s: float | None) -> None:
    async with db.begin() as conn:
        await conn.execute(sa.text("DELETE FROM worker_heartbeats"))
        if age_s is not None:
            await conn.execute(
                sa.text(
                    "INSERT INTO worker_heartbeats VALUES ('w', now() - CAST(:s AS double precision)"
                    " * interval '1 second')"
                ),
                {"s": age_s},
            )


async def test_livez_is_starting_within_grace_even_with_stale_heartbeat(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    await set_heartbeat(db, 3600)
    r = await client.get("/livez")
    assert r.status_code == 200 and r.json() == {"status": "ok", "worker": "starting"}


@pytest.fixture
def past_grace(app: object) -> None:
    app.state.started_monotonic -= 1000  # type: ignore[attr-defined]


@pytest.mark.usefixtures("past_grace")
async def test_livez_after_grace(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await set_heartbeat(db, 5)
    r = await client.get("/livez")
    assert r.status_code == 200 and r.json()["worker"] == "ok"
    await set_heartbeat(db, 61)
    r = await client.get("/livez")
    assert r.status_code == 503 and r.json()["worker"] == "stale"
    await set_heartbeat(db, None)
    assert (await client.get("/livez")).status_code == 503  # worker never started


async def test_livez_unknown_when_database_is_unreachable(settings: Settings) -> None:
    dead = create_async_engine("postgresql+asyncpg://x:x@127.0.0.1:1/x")
    app = create_app(settings, engine=dead)
    app.state.started_monotonic -= 1000
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/livez")
        assert r.status_code == 200 and r.json() == {"status": "ok", "worker": "unknown"}
        h = await c.get("/health")
        assert h.status_code == 200 and h.json()["database"] == "unreachable"
    await dead.dispose()


async def test_health_reports_counts(
    client: httpx.AsyncClient, db: AsyncEngine, sender_key: object
) -> None:
    from tests.integration.helpers import submit

    await set_heartbeat(db, 2)
    h = (await client.get("/health")).json()
    assert h["database"] == "ok" and h["pending"] == 0 and h["failed"] == 0
    assert 1 <= h["worker_heartbeat_age_s"] < 10
    await submit(client, sender_key, {"a": 1})  # type: ignore[arg-type]
    await submit(client, sender_key, {"a": 2})  # type: ignore[arg-type]
    async with db.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE ingestions SET status='failed' WHERE id = (SELECT id FROM ingestions LIMIT 1)"
            )
        )
    h = (await client.get("/health")).json()
    assert (h["pending"], h["processing"], h["failed"]) == (1, 0, 1)


async def test_health_without_heartbeat(client: httpx.AsyncClient) -> None:
    h = await client.get("/health")
    assert h.status_code == 200 and h.json()["worker_heartbeat_age_s"] is None
