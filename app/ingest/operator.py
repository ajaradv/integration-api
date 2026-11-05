"""Operator actions: promote a quarantined ingestion, reprocess stored payloads."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.models import PromoteRequest
from app.errors import ApiError
from app.ingest import repository as repo
from app.transform import transform

REPROCESSABLE = ("rejected", "failed", "quarantined")
BULK_MAX = 500

_RESET = (
    "status = 'pending', attempts = 0, next_attempt_at = NULL, claimed_at = NULL, "
    "last_error = NULL, processed_at = NULL, "
    "issues_history = issues_history || jsonb_build_array(jsonb_build_object("
    "'at', now(), 'reason', 'reprocess', 'previous_status', status, 'source', CAST(:src AS text), "
    "'issues', COALESCE(issues, '[]'::jsonb))), "
    "issues = NULL, candidate_sensor = NULL, candidate_location = NULL, write_result = NULL, "
    "reprocess_count = reprocess_count + 1, reprocessed_by = :by, reprocessed_at = now()"
)


async def promote(
    conn: AsyncConnection, id_: UUID, req: PromoteRequest, operator_key_id: UUID
) -> dict[str, Any]:
    row = (
        await conn.execute(
            sa.text("SELECT status, raw_json, issues FROM ingestions WHERE id = :id FOR UPDATE"),
            {"id": id_},
        )
    ).first()
    if row is None:
        raise ApiError(404, "not_found", "ingestion not found")
    if row.status != "quarantined":
        raise ApiError(
            409,
            "not_quarantined",
            f"only quarantined ingestions can be promoted; this one is {row.status}",
        )
    overrides = req.overrides.to_dict()
    result = transform(row.raw_json, overrides, int(time.time() * 1000))
    if result.status_hint == "rejected" or result.blocking:
        raise ApiError(
            422,
            "promotion_blocked",
            "blocking issues remain after applying the overrides",
            details={"issues": [i.to_dict() for i in result.issues]},
        )
    assert result.px_sensor is not None and result.px_location is not None
    assert result.extras is not None
    outcome = await repo.upsert_reading(
        conn,
        result.px_sensor,
        result.px_location,
        result.extras,
        id_,
        allow_replace_foreign=req.replace_existing,
    )
    if outcome.result == "conflict":
        raise ApiError(
            409,
            "conflicts_existing",
            f"a reading with different content already exists, owned by ingestion "
            f"{outcome.owner_ingestion_id}; pass replace_existing: true to replace it",
            details={"owner_ingestion_id": str(outcome.owner_ingestion_id)},
        )
    write_result: dict[str, Any] = {
        "result": outcome.result,
        "device_imei": result.px_sensor["device_imei"],
        "timestamp_ms": result.px_sensor["timestamp"],
    }
    if outcome.result == "unchanged":
        write_result["owner_ingestion_id"] = str(outcome.owner_ingestion_id)
    old_issues = row.issues or []
    await repo.write_ingestion_result(
        conn,
        id_,
        status="promoted",
        issues=result.issues,
        candidate_sensor=None,
        candidate_location=None,
        write_result=write_result,
        extras=result.extras,
        extra_sets=(
            ", promoted_by = :pby, promoted_at = now(), "
            "promotion_overrides = CAST(:po AS jsonb), "
            "issues_history = issues_history || jsonb_build_array(jsonb_build_object("
            "'at', now(), 'reason', 'promotion', 'previous_status', 'quarantined', "
            "'source', 'api', 'issues', CAST(:old AS jsonb)))"
        ),
        extra_params={
            "pby": operator_key_id,
            "po": repo.dumps(
                {
                    "overrides": overrides,
                    "note": req.note,
                    "replace_existing": req.replace_existing,
                }
            ),
            "old": repo.dumps(old_issues),
        },
    )
    views = await repo.get_ingestion_views(conn, [id_])
    return views[0]


async def reprocess(
    conn: AsyncConnection, id_: UUID, operator_key_id: UUID | None, source: str = "api"
) -> None:
    """Guarded reset to `pending`. Raises 404/409 when the row is missing or not eligible."""
    updated = (
        await conn.execute(
            sa.text(
                f"UPDATE ingestions SET {_RESET} "
                "WHERE id = :id AND status IN ('rejected', 'failed', 'quarantined') RETURNING id"
            ),
            {"id": id_, "by": operator_key_id, "src": source},
        )
    ).first()
    if updated is not None:
        return
    exists = (
        await conn.execute(sa.text("SELECT status FROM ingestions WHERE id = :id"), {"id": id_})
    ).first()
    if exists is None:
        raise ApiError(404, "not_found", "ingestion not found")
    raise ApiError(
        409,
        "not_reprocessable",
        f"only rejected, failed or quarantined ingestions can be reprocessed; "
        f"this one is {exists.status}",
    )


async def reprocess_bulk(
    conn: AsyncConnection,
    status: str,
    limit: int,
    operator_key_id: UUID | None,
    source: str = "api",
) -> int:
    if status not in REPROCESSABLE:
        raise ApiError(422, "validation_error", f"status must be one of {', '.join(REPROCESSABLE)}")
    limit = max(1, min(limit, BULK_MAX))
    result = await conn.execute(
        sa.text(
            f"UPDATE ingestions SET {_RESET} WHERE id IN ("
            "  SELECT id FROM ingestions WHERE status = :st "
            "  ORDER BY received_at LIMIT :lim FOR UPDATE SKIP LOCKED) RETURNING id"
        ),
        {"st": status, "lim": limit, "by": operator_key_id, "src": source},
    )
    return len(result.all())
