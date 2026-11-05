from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.transform.output_validation import validate_px
from tests.fixtures.expected_outcomes import EXPECTED
from tests.integration.helpers import submit, view


async def test_all_13_samples_end_to_end(
    client: httpx.AsyncClient,
    sender_key: Any,
    samples: list[dict[str, Any]],
    drain: Callable[[], Awaitable[int]],
    db: AsyncEngine,
) -> None:
    ids = [(await submit(client, sender_key, s))["ingestion_id"] for s in samples]
    assert await drain() == 13

    views = [await view(client, sender_key, i) for i in ids]
    for n, v in enumerate(views, start=1):
        expected = EXPECTED[n]
        assert v["status"] == expected["status"], f"sample {n}"
        blocking = [i["code"] for i in v["issues"] if i["severity"] == "blocking"]
        assert blocking == expected["blocking"], f"sample {n}"
        warnings = [i["code"] for i in v["issues"] if i["severity"] == "warning"]
        assert sorted(warnings) == sorted(expected["warning"]), f"sample {n}"

    # sample 8 (index 7) has a different timestamp from sample 1, so it replaced nothing
    assert views[7]["write_result"]["result"] == "inserted"
    # 8 readings, 5 quarantined (design table)
    accepted = [v for v in views if v["status"] in ("accepted", "accepted_with_corrections")]
    async with db.connect() as conn:
        rows = (await conn.execute(sa.text("SELECT px_sensor, px_location FROM readings"))).all()
        history = (
            await conn.execute(sa.text("SELECT count(*) FROM readings_history"))
        ).scalar_one()
    assert len(rows) == len(accepted) == 8
    assert history == 0
    for r in rows:
        assert validate_px("sensor", r.px_sensor) == []
        assert validate_px("location", r.px_location) == []
    quarantined = [v for v in views if v["status"] == "quarantined"]
    assert len(quarantined) == 5
    assert all(v["candidate_sensor"] is not None and v["readings"] == [] for v in quarantined)
    assert views[9]["write_result"]["result"] == "inserted"
    async with db.connect() as conn:
        acct = (
            await conn.execute(
                sa.text("SELECT account_id FROM readings WHERE device_imei='866088073468444'")
            )
        ).scalar_one()
    assert acct == 999
