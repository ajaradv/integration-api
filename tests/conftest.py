from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
NOW_MS = 1740787200000  # 2025-03-01T00:00:00Z


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        path = str(item.fspath)
        if f"{os.sep}unit{os.sep}" in path:
            item.add_marker(pytest.mark.unit)
        elif f"{os.sep}integration{os.sep}" in path:
            item.add_marker(pytest.mark.integration)
            if not os.environ.get("TEST_DATABASE_URL"):
                item.add_marker(pytest.mark.skip(reason="TEST_DATABASE_URL is not set"))


@pytest.fixture(scope="session")
def samples() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "sample-tive-payloads.json").read_text(encoding="utf-8"))
    return [p["payload"] for p in data["payloads"]]


@pytest.fixture
def now_ms() -> int:
    return NOW_MS


# Integration fixtures (db, client, keys, worker) live in tests/integration/conftest.py.


# --------------------------------------------------------------------------- integration

import asyncio  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from app.auth import generate_key  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ingest import repository as repo  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker import loop as worker_loop  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TestKey:
    __test__ = False
    id: Any
    plaintext: str
    role: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.plaintext}


@pytest.fixture(scope="session")
def settings() -> Settings:
    url = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://x:x@localhost:1/x")
    return Settings(database_url=url, livez_grace_s=90, log_level="WARNING", _env_file=None)  # type: ignore[call-arg]


def _migrate(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(_migrate, settings.database_url)
    eng = create_async_engine(settings.database_url, pool_size=10, max_overflow=10)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncEngine:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "TRUNCATE readings_history, readings, ingestions, worker_heartbeats, api_keys "
                "RESTART IDENTITY CASCADE"
            )
        )
    return engine


async def _make_key(engine: AsyncEngine, role: str, name: str) -> TestKey:
    plaintext, digest = generate_key()
    async with engine.begin() as conn:
        key_id = (
            await conn.execute(
                sa.text(
                    "INSERT INTO api_keys (name, key_hash, role) VALUES (:n, :h, :r) RETURNING id"
                ),
                {"n": name, "h": digest, "r": role},
            )
        ).scalar_one()
    return TestKey(key_id, plaintext, role)


@pytest_asyncio.fixture
async def sender_key(db: AsyncEngine) -> TestKey:
    return await _make_key(db, "sender", "test-sender")


@pytest_asyncio.fixture
async def operator_key(db: AsyncEngine) -> TestKey:
    return await _make_key(db, "operator", "test-operator")


@pytest_asyncio.fixture
async def make_key(db: AsyncEngine) -> Callable[[str, str], Awaitable[TestKey]]:
    async def factory(role: str, name: str) -> TestKey:
        return await _make_key(db, role, name)

    return factory


@pytest_asyncio.fixture
async def app(db: AsyncEngine, settings: Settings) -> Any:
    return create_app(settings, engine=db)


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def run_worker_once(db: AsyncEngine, settings: Settings) -> Callable[[], Awaitable[int]]:
    """Claim a batch and process it with process_one. Returns the number of rows claimed."""

    async def once() -> int:
        async with db.begin() as conn:
            rows = await repo.claim_batch(conn, settings.worker_max_jobs, settings.max_attempts)
        for row in rows:
            await worker_loop.process_one(db, settings, row, NOW_MS)
        return len(rows)

    return once


@pytest_asyncio.fixture
async def drain(run_worker_once: Callable[[], Awaitable[int]]) -> Callable[[], Awaitable[int]]:
    """Run the worker until nothing is claimable. Returns the total processed."""

    async def run_all() -> int:
        total = 0
        while (n := await run_worker_once()) > 0:
            total += n
        return total

    return run_all
