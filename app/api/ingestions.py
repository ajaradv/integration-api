from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.models import (
    IngestionBatch,
    IngestionPage,
    IngestionView,
    PromoteRequest,
)
from app.auth import ApiKey, require_role
from app.db import get_conn
from app.errors import ApiError
from app.ingest import operator
from app.ingest import repository as repo

router = APIRouter(prefix="/api/ingestions", tags=["ingestions"])

reader = require_role("sender", "operator")
operator_only = require_role("operator")

VALID_STATUSES = {
    "pending",
    "processing",
    "accepted",
    "accepted_with_corrections",
    "quarantined",
    "rejected",
    "promoted",
    "failed",
}


@router.get("/batch", response_model=IngestionBatch)
async def batch(
    ids: Annotated[list[str], Query(description="Up to 100 ids, comma separated or repeated")],
    key: ApiKey = Depends(reader),
    conn: AsyncConnection = Depends(get_conn),
) -> IngestionBatch:
    parts = [p.strip() for raw in ids for p in raw.split(",") if p.strip()]
    if len(parts) > 100:
        raise ApiError(422, "too_many_ids", "at most 100 ids per request")
    try:
        uuids = list(dict.fromkeys(UUID(p) for p in parts))
    except ValueError:
        raise ApiError(422, "validation_error", "ids must be UUIDs") from None
    scope = key.id if key.role == "sender" else None
    views = await repo.get_ingestion_views(conn, uuids, scope)
    return IngestionBatch(items=[IngestionView(**v) for v in views])


@router.get("", response_model=IngestionPage)
async def list_ingestions(
    status: str | None = None,
    device_imei: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    _: ApiKey = Depends(operator_only),
    conn: AsyncConnection = Depends(get_conn),
) -> IngestionPage:
    if status is not None and status not in VALID_STATUSES:
        raise ApiError(422, "validation_error", f"unknown status {status!r}")
    decoded = None
    if cursor:
        try:
            decoded = repo.decode_cursor(cursor)
        except Exception:
            raise ApiError(422, "invalid_cursor", "cursor is not valid") from None
    rows, next_cursor = await repo.list_ingestions(
        conn, status=status, device_imei=device_imei, limit=limit, cursor=decoded
    )
    return IngestionPage(items=[IngestionView(**r) for r in rows], next_cursor=next_cursor)


@router.post("/reprocess")
async def reprocess_bulk(
    status: str,
    limit: Annotated[int, Query(ge=1, le=operator.BULK_MAX)] = operator.BULK_MAX,
    key: ApiKey = Depends(operator_only),
    conn: AsyncConnection = Depends(get_conn),
) -> dict[str, int]:
    count = await operator.reprocess_bulk(conn, status, limit, key.id)
    await conn.commit()
    return {"queued": count}


@router.get("/{ingestion_id}", response_model=IngestionView)
async def get_ingestion(
    ingestion_id: UUID,
    _: ApiKey = Depends(reader),
    conn: AsyncConnection = Depends(get_conn),
) -> IngestionView:
    view = await repo.get_ingestion_view(conn, ingestion_id)
    if view is None:
        raise ApiError(404, "not_found", "ingestion not found")
    return IngestionView(**view)


@router.post("/{ingestion_id}/promote", response_model=IngestionView)
async def promote(
    ingestion_id: UUID,
    req: PromoteRequest,
    key: ApiKey = Depends(operator_only),
    conn: AsyncConnection = Depends(get_conn),
) -> IngestionView:
    view = await operator.promote(conn, ingestion_id, req, key.id)
    await conn.commit()
    return IngestionView(**view)


@router.post("/{ingestion_id}/reprocess")
async def reprocess(
    ingestion_id: UUID,
    key: ApiKey = Depends(operator_only),
    conn: AsyncConnection = Depends(get_conn),
) -> dict[str, str]:
    await operator.reprocess(conn, ingestion_id, key.id)
    await conn.commit()
    return {"ingestion_id": str(ingestion_id), "status": "pending"}
