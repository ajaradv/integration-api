from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.models import WebhookAccepted
from app.auth import ApiKey, require_role
from app.db import get_conn
from app.errors import ApiError
from app.ingest import repository as repo
from app.ingest.hashing import MalformedBody, canonical_hash, raw_hash, strict_parse
from app.transform.issues import Issue, IssueCode, Severity

router = APIRouter()
sender_only = require_role("sender")
log = structlog.get_logger("webhook")

_IDEMPOTENCY_KEY = re.compile(r"[\x20-\x7e]{1,255}")


@router.post(
    "/api/webhook/tive",
    status_code=202,
    response_model=WebhookAccepted,
    responses={
        200: {"description": "Duplicate delivery: the existing ingestion"},
        400: {"description": "Bad Idempotency-Key, or the body is not valid JSON"},
        401: {"description": "Missing, unknown or revoked API key"},
        409: {"description": "Idempotency-Key reused with a different body"},
        413: {"description": "Body larger than the limit"},
    },
)
async def receive_tive(
    request: Request,
    key: ApiKey = Depends(sender_only),
    conn: AsyncConnection = Depends(get_conn),
) -> JSONResponse:
    idem = request.headers.get("idempotency-key")
    if idem is not None and not _IDEMPOTENCY_KEY.fullmatch(idem):
        raise ApiError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key must be 1-255 printable ASCII characters",
        )
    body = await request.body()
    parsed = strict_parse(body)
    issues: list[Issue] = []
    if isinstance(parsed, MalformedBody):
        body_hash = raw_hash(body)
        status = "rejected"
        raw_json = None
        issues.append(Issue(IssueCode.MALFORMED_JSON, "$", Severity.BLOCKING, parsed.reason))
    else:
        body_hash = canonical_hash(parsed)
        status = "pending"
        raw_json = parsed
    structlog.contextvars.bind_contextvars(api_key_id=str(key.id), body_hash=body_hash)

    try:
        row, created = await repo.insert_ingestion(
            conn,
            api_key_id=key.id,
            body_hash=body_hash,
            idempotency_key=idem,
            raw_body=body,
            raw_json=raw_json,
            status=status,
            issues=issues,
        )
    except repo.IdempotencyKeyReused:
        raise ApiError(
            409,
            "idempotency_key_reused",
            "this Idempotency-Key was already used with a different body",
        ) from None
    await conn.commit()

    ingestion_id = row["id"]
    structlog.contextvars.bind_contextvars(ingestion_id=str(ingestion_id))
    status_url = f"/api/ingestions/{ingestion_id}"
    log.info("webhook_received", created=created, status=row["status"])
    payload = WebhookAccepted(
        ingestion_id=ingestion_id,
        status=row["status"],
        status_url=status_url,
        duplicate=not created,
    ).model_dump(mode="json")
    if row["status"] == "rejected":
        message = issues[0].message if issues else "body is not valid JSON"
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "malformed_json",
                    "message": message,
                    "details": {"ingestion_id": str(ingestion_id)},
                },
                **payload,
            },
        )
    return JSONResponse(status_code=202 if created else 200, content=payload)
