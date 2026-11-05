from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app import cli
from app.config import Settings
from tests.integration.helpers import submit


@pytest.fixture(autouse=True)
def _cli_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch, db: AsyncEngine) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


async def run_cli(*argv: str) -> int:
    return await asyncio.to_thread(cli.main, list(argv))


async def test_create_list_revoke_key(
    client: httpx.AsyncClient,
    samples: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    db: AsyncEngine,
) -> None:
    assert await run_cli("create-key", "--name", "laptop", "--role", "sender") == 0
    out = capsys.readouterr().out
    plaintext = next(line.split()[1] for line in out.splitlines() if line.startswith("key:"))
    key_id = next(line.split()[1] for line in out.splitlines() if line.startswith("id:"))
    assert plaintext.startswith("pxk_")
    async with db.connect() as conn:
        stored = (await conn.execute(sa.text("SELECT key_hash FROM api_keys"))).scalar_one()
    assert stored != plaintext and len(stored) == 64  # only the hash is stored

    r = await client.post("/api/webhook/tive", json=samples[0], headers={"X-API-Key": plaintext})
    assert r.status_code == 202
    assert await run_cli("list-keys") == 0
    assert "laptop" in capsys.readouterr().out
    assert await run_cli("revoke-key", "--id", key_id) == 0
    assert await run_cli("revoke-key", "--id", key_id) == 1
    r = await client.post("/api/webhook/tive", json=samples[2], headers={"X-API-Key": plaintext})
    assert r.status_code == 401


async def test_cli_reprocess_single_and_bulk(
    client: httpx.AsyncClient,
    sender_key: Any,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
    db: AsyncEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = (await submit(client, sender_key, samples[1]))["ingestion_id"]
    await submit(client, sender_key, samples[4])
    await drain()
    assert await run_cli("reprocess", "--id", a) == 0
    assert await run_cli("reprocess", "--id", a) == 1  # now pending: not reprocessable
    assert await run_cli("reprocess", "--status", "quarantined", "--limit", "10") == 0
    assert "queued 1" in capsys.readouterr().out
    async with db.connect() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT issues_history, reprocessed_by FROM ingestions WHERE id = :i"),
                {"i": a},
            )
        ).one()
    assert row.issues_history[0]["source"] == "cli" and row.reprocessed_by is None
