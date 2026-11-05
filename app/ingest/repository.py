"""All SQL for ingestion, the queue and readings."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from app.transform.issues import Issue
from app.transform.mapping import SensorExtras
from app.transform.pipeline import PipelineResult


class IdempotencyKeyReused(Exception):
    """The same Idempotency-Key was sent with a different body."""


def dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# --------------------------------------------------------------------------- webhook


async def insert_ingestion(
    conn: AsyncConnection,
    *,
    api_key_id: UUID,
    body_hash: str,
    idempotency_key: str | None,
    raw_body: bytes,
    raw_json: Any,
    status: str,
    issues: list[Issue] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert once; on any unique conflict return the existing row. Returns (row, created)."""
    params = {
        "k": api_key_id,
        "h": body_hash,
        "i": idempotency_key,
        "b": raw_body,
        "rj": None if status == "rejected" else dumps(raw_json),
        "s": status,
        "iss": dumps([i.to_dict() for i in issues]) if issues else None,
        "done": status == "rejected",
    }
    inserted = (
        await conn.execute(
            sa.text(
                "INSERT INTO ingestions (api_key_id, body_hash, idempotency_key, raw_body, "
                "raw_json, status, issues, processed_at) "
                "VALUES (:k, :h, :i, :b, CAST(:rj AS jsonb), :s, CAST(:iss AS jsonb), "
                "CASE WHEN CAST(:done AS boolean) THEN now() END) "
                "ON CONFLICT DO NOTHING RETURNING id, status, body_hash"
            ),
            params,
        )
    ).first()
    if inserted is not None:
        return dict(inserted._mapping), True
    if idempotency_key is not None:
        existing = (
            await conn.execute(
                sa.text(
                    "SELECT id, status, body_hash FROM ingestions "
                    "WHERE api_key_id = :k AND idempotency_key = :i"
                ),
                {"k": api_key_id, "i": idempotency_key},
            )
        ).first()
    else:
        existing = (
            await conn.execute(
                sa.text(
                    "SELECT id, status, body_hash FROM ingestions "
                    "WHERE api_key_id = :k AND body_hash = :h AND idempotency_key IS NULL"
                ),
                {"k": api_key_id, "h": body_hash},
            )
        ).first()
    if existing is None:  # pragma: no cover - the conflicting row vanished; extremely unlikely
        raise RuntimeError("ingestion conflict without an existing row")
    row = dict(existing._mapping)
    if idempotency_key is not None and row["body_hash"] != body_hash:
        raise IdempotencyKeyReused(idempotency_key)
    return row, False


# --------------------------------------------------------------------------- queue


async def claim_batch(conn: AsyncConnection, n: int, max_attempts: int) -> list[dict[str, Any]]:
    result = await conn.execute(
        sa.text(
            "UPDATE ingestions SET status = 'processing', attempts = attempts + 1, "
            "claimed_at = now() WHERE id IN ("
            "  SELECT id FROM ingestions WHERE status = 'pending' AND attempts < :max "
            "  AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
            "  ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT :n) "
            "RETURNING id, raw_json, (raw_json IS NULL) AS malformed, attempts, reprocess_count"
        ),
        {"n": n, "max": max_attempts},
    )
    return [dict(r._mapping) for r in result]


async def release_for_retry(conn: AsyncConnection, id_: UUID, error: str, attempts: int) -> None:
    await conn.execute(
        sa.text(
            "UPDATE ingestions SET status = 'pending', last_error = :e, "
            "next_attempt_at = now() + CAST(:secs AS double precision) * interval '1 second' "
            "WHERE id = :id AND status = 'processing'"
        ),
        {"id": id_, "e": error[:2000], "secs": float(2**attempts)},
    )


async def mark_failed(conn: AsyncConnection, id_: UUID, error: str) -> None:
    await conn.execute(
        sa.text(
            "UPDATE ingestions SET status = 'failed', last_error = :e, processed_at = now() "
            "WHERE id = :id AND status = 'processing'"
        ),
        {"id": id_, "e": error[:2000]},
    )


async def reclaim_stale(
    conn: AsyncConnection, stale_s: float, max_attempts: int
) -> tuple[int, int]:
    """Return (released, failed) counts for rows abandoned in `processing`."""
    row = (
        await conn.execute(
            sa.text(
                "WITH stale AS ("
                "  SELECT id, attempts FROM ingestions WHERE status = 'processing' "
                "  AND claimed_at < now() - CAST(:s AS double precision) * interval '1 second' "
                "  FOR UPDATE SKIP LOCKED), "
                "upd AS ("
                "  UPDATE ingestions i SET "
                "    status = CASE WHEN s.attempts < :max THEN 'pending' ELSE 'failed' END, "
                "    last_error = CASE WHEN s.attempts < :max THEN 'claim expired' "
                "      ELSE 'attempts exhausted' END, "
                "    processed_at = CASE WHEN s.attempts < :max THEN NULL ELSE now() END "
                "  FROM stale s WHERE i.id = s.id RETURNING i.status) "
                "SELECT count(*) FILTER (WHERE status = 'pending') AS released, "
                "       count(*) FILTER (WHERE status = 'failed') AS failed FROM upd"
            ),
            {"s": float(stale_s), "max": max_attempts},
        )
    ).one()
    return int(row.released), int(row.failed)


async def beat(conn: AsyncConnection, worker_id: str) -> None:
    await conn.execute(
        sa.text(
            "INSERT INTO worker_heartbeats (worker_id, beat_at) VALUES (:w, now()) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = EXCLUDED.beat_at"
        ),
        {"w": worker_id},
    )


# --------------------------------------------------------------------------- persist

_CONTENT_SAME = (
    "px_sensor = CAST(:s AS jsonb) AND px_location = CAST(:l AS jsonb) "
    "AND probe_temperature IS NOT DISTINCT FROM CAST(:p AS numeric) AND probe_fault = :pf"
)


@dataclass(frozen=True)
class UpsertOutcome:
    result: str  # inserted | unchanged | replaced | conflict
    owner_ingestion_id: UUID | None = None  # the ingestion that owns the existing row


async def upsert_reading(
    conn: AsyncConnection,
    px_sensor: dict[str, Any],
    px_location: dict[str, Any],
    extras: SensorExtras,
    ingestion_id: UUID,
    allow_replace_foreign: bool,
) -> UpsertOutcome:
    """Insert-first upsert of the single readings row, with history on replacement.

    Runs inside a savepoint: on "conflict" nothing has been written and the savepoint is
    rolled back, so the caller can persist the ingestion as quarantined.
    """
    params = {
        "ing": ingestion_id,
        "s": dumps(px_sensor),
        "l": dumps(px_location),
        "p": _dec(extras.probe_temperature),
        "pf": extras.probe_fault,
        "acc": extras.account_id,
        "en": extras.entity_name,
        "sh": extras.shipment_id,
        "imei": px_sensor["device_imei"],
        "ts": px_sensor["timestamp"],
    }
    savepoint = await conn.begin_nested()
    try:
        inserted = (
            await conn.execute(
                sa.text(
                    "INSERT INTO readings (ingestion_id, px_sensor, px_location, "
                    "probe_temperature, probe_fault, account_id, entity_name, shipment_id) "
                    "VALUES (:ing, CAST(:s AS jsonb), CAST(:l AS jsonb), CAST(:p AS numeric), "
                    ":pf, :acc, :en, :sh) "
                    "ON CONFLICT (device_imei, timestamp_ms) DO NOTHING RETURNING id"
                ),
                params,
            )
        ).first()
        if inserted is not None:
            await savepoint.commit()
            return UpsertOutcome("inserted")
        existing = (
            await conn.execute(
                sa.text(
                    f"SELECT id, ingestion_id, ({_CONTENT_SAME}) AS same FROM readings "
                    "WHERE device_imei = :imei AND timestamp_ms = :ts FOR UPDATE"
                ),
                params,
            )
        ).one()
        owner: UUID = existing.ingestion_id
        if existing.same:
            await savepoint.commit()
            return UpsertOutcome("unchanged", owner)
        if owner != ingestion_id and not allow_replace_foreign:
            await savepoint.rollback()
            return UpsertOutcome("conflict", owner)
        await conn.execute(
            sa.text(
                "INSERT INTO readings_history (reading_id, ingestion_id, px_sensor, px_location, "
                "device_imei, timestamp_ms, temperature, latitude, longitude, probe_temperature, "
                "probe_fault, account_id, entity_name, shipment_id, created_at, updated_at, "
                "replaced_by_ingestion_id) "
                "SELECT id, ingestion_id, px_sensor, px_location, device_imei, timestamp_ms, "
                "temperature, latitude, longitude, probe_temperature, probe_fault, account_id, "
                "entity_name, shipment_id, created_at, updated_at, :ing "
                "FROM readings WHERE id = :rid"
            ),
            {"ing": ingestion_id, "rid": existing.id},
        )
        await conn.execute(
            sa.text(
                "UPDATE readings SET ingestion_id = :ing, px_sensor = CAST(:s AS jsonb), "
                "px_location = CAST(:l AS jsonb), probe_temperature = CAST(:p AS numeric), "
                "probe_fault = :pf, account_id = :acc, entity_name = :en, shipment_id = :sh, "
                "updated_at = now() WHERE id = :rid"
            ),
            {**params, "rid": existing.id},
        )
        await savepoint.commit()
        return UpsertOutcome("replaced", owner)
    except BaseException:
        if savepoint.is_active:
            await savepoint.rollback()
        raise


async def write_ingestion_result(
    conn: AsyncConnection,
    ingestion_id: UUID,
    *,
    status: str,
    issues: list[Issue],
    candidate_sensor: dict[str, Any] | None,
    candidate_location: dict[str, Any] | None,
    write_result: dict[str, Any] | None,
    extras: SensorExtras | None,
    extra_sets: str = "",
    extra_params: dict[str, Any] | None = None,
) -> None:
    imei = (candidate_sensor or {}).get("device_imei") or (write_result or {}).get("device_imei")
    ts = (candidate_sensor or {}).get("timestamp") or (write_result or {}).get("timestamp_ms")
    await conn.execute(
        sa.text(
            "UPDATE ingestions SET status = :st, issues = CAST(:iss AS jsonb), "
            "candidate_sensor = CAST(:cs AS jsonb), candidate_location = CAST(:cl AS jsonb), "
            "write_result = CAST(:wr AS jsonb), processed_at = now(), last_error = NULL, "
            "device_imei = :imei, reading_timestamp_ms = :ts, account_id = :acc"
            f"{extra_sets} WHERE id = :id"
        ),
        {
            "id": ingestion_id,
            "st": status,
            "iss": dumps([i.to_dict() for i in issues]),
            "cs": dumps(candidate_sensor),
            "cl": dumps(candidate_location),
            "wr": dumps(write_result),
            "imei": imei if isinstance(imei, str) else None,
            "ts": ts if isinstance(ts, int) else None,
            "acc": extras.account_id if extras else None,
            **(extra_params or {}),
        },
    )


async def persist_result(
    conn: AsyncConnection,
    *,
    ingestion_id: UUID,
    result: PipelineResult,
    allow_replace_foreign: bool,
) -> str:
    """Write the outcome of one transform. Returns the final status."""
    from app.transform.issues import IssueCode, Severity

    issues = list(result.issues)
    if result.status_hint in ("rejected", "quarantined"):
        await write_ingestion_result(
            conn,
            ingestion_id,
            status=result.status_hint,
            issues=issues,
            candidate_sensor=result.px_sensor if result.status_hint == "quarantined" else None,
            candidate_location=result.px_location if result.status_hint == "quarantined" else None,
            write_result=None,
            extras=result.extras,
        )
        return result.status_hint

    assert result.px_sensor is not None and result.px_location is not None
    assert result.extras is not None
    outcome = await upsert_reading(
        conn,
        result.px_sensor,
        result.px_location,
        result.extras,
        ingestion_id,
        allow_replace_foreign,
    )
    if outcome.result == "conflict":
        issues.append(
            Issue(
                IssueCode.CONFLICTS_EXISTING,
                "readings",
                Severity.BLOCKING,
                "a reading with different content already exists for this device and "
                f"timestamp, owned by ingestion {outcome.owner_ingestion_id}; replacing it "
                "requires promotion with replace_existing",
                original=str(outcome.owner_ingestion_id),
            )
        )
        await write_ingestion_result(
            conn,
            ingestion_id,
            status="quarantined",
            issues=issues,
            candidate_sensor=result.px_sensor,
            candidate_location=result.px_location,
            write_result=None,
            extras=result.extras,
        )
        return "quarantined"
    write_result: dict[str, Any] = {
        "result": outcome.result,
        "device_imei": result.px_sensor["device_imei"],
        "timestamp_ms": result.px_sensor["timestamp"],
    }
    if outcome.result == "unchanged":
        write_result["owner_ingestion_id"] = str(outcome.owner_ingestion_id)
    await write_ingestion_result(
        conn,
        ingestion_id,
        status=result.status_hint,
        issues=issues,
        candidate_sensor=None,
        candidate_location=None,
        write_result=write_result,
        extras=result.extras,
    )
    return result.status_hint


# --------------------------------------------------------------------------- reads

_VIEW_COLUMNS = (
    "id, status, received_at, processed_at, attempts, issues, issues_history, "
    "candidate_sensor, candidate_location, write_result, promoted_at, reprocess_count"
)


def _view(
    row: Any, readings: dict[tuple[str, int], Any], history: dict[Any, Any]
) -> dict[str, Any]:
    wr = row.write_result
    views: list[dict[str, Any]] = []
    probe_temperature: float | None = None
    probe_fault: bool | None = None
    if wr:
        key = (wr["device_imei"], int(wr["timestamp_ms"]))
        current = readings.get(key)
        owner = (
            row.id if wr["result"] in ("inserted", "replaced") else UUID(wr["owner_ingestion_id"])
        )
        source = None
        is_current = False
        superseded_by = None
        if current is not None and current.ingestion_id == owner:
            source, is_current = current, True
        else:
            old = history.get((owner, *key))
            if old is not None:
                source, superseded_by = old, old.replaced_by_ingestion_id
            elif current is not None:
                source, superseded_by = current, current.ingestion_id
        if source is not None:
            for kind, payload in (("sensor", source.px_sensor), ("location", source.px_location)):
                views.append(
                    {
                        "kind": kind,
                        "px_payload": payload,
                        "current": is_current,
                        "superseded_by": superseded_by,
                    }
                )
            pt = source.probe_temperature
            probe_temperature = None if pt is None else float(pt)
            probe_fault = source.probe_fault
    return {
        "id": row.id,
        "status": row.status,
        "received_at": row.received_at,
        "processed_at": row.processed_at,
        "attempts": row.attempts,
        "issues": row.issues or [],
        "issues_history": row.issues_history or [],
        "readings": views,
        "candidate_sensor": row.candidate_sensor,
        "candidate_location": row.candidate_location,
        "write_result": wr,
        "probe_temperature": probe_temperature,
        "probe_fault": probe_fault,
        "promoted_at": row.promoted_at,
        "reprocess_count": row.reprocess_count,
    }


async def _build_views(conn: AsyncConnection, rows: list[Any]) -> list[dict[str, Any]]:
    keys = sorted(
        {
            (r.write_result["device_imei"], int(r.write_result["timestamp_ms"]))
            for r in rows
            if r.write_result
        }
    )
    readings: dict[tuple[str, int], Any] = {}
    history: dict[Any, Any] = {}
    if keys:
        key_params = {"imeis": [k[0] for k in keys], "tss": [k[1] for k in keys]}
        pair = (
            "(device_imei, timestamp_ms) IN "
            "(SELECT * FROM unnest(CAST(:imeis AS text[]), CAST(:tss AS bigint[])))"
        )
        for r in await conn.execute(
            sa.text(
                "SELECT device_imei, timestamp_ms, ingestion_id, px_sensor, px_location, "
                f"probe_temperature, probe_fault FROM readings WHERE {pair}"
            ),
            key_params,
        ):
            readings[(r.device_imei, r.timestamp_ms)] = r
        for r in await conn.execute(
            sa.text(
                "SELECT device_imei, timestamp_ms, ingestion_id, px_sensor, px_location, "
                "probe_temperature, probe_fault, replaced_by_ingestion_id, replaced_at "
                f"FROM readings_history WHERE {pair} ORDER BY replaced_at, id"
            ),
            key_params,
        ):
            # ORDER BY ascending: the LAST write wins, i.e. the latest version by that owner
            history[(r.ingestion_id, r.device_imei, r.timestamp_ms)] = r
    return [_view(r, readings, history) for r in rows]


async def get_ingestion_views(
    conn: AsyncConnection, ids: list[UUID], api_key_scope: UUID | None = None
) -> list[dict[str, Any]]:
    sql = f"SELECT {_VIEW_COLUMNS} FROM ingestions WHERE id = ANY(CAST(:ids AS uuid[]))"
    params: dict[str, Any] = {"ids": ids}
    if api_key_scope is not None:
        sql += " AND api_key_id = :scope"
        params["scope"] = api_key_scope
    rows = (await conn.execute(sa.text(sql), params)).all()
    views = await _build_views(conn, list(rows))
    order = {id_: n for n, id_ in enumerate(ids)}
    return sorted(views, key=lambda v: order[v["id"]])


async def get_ingestion_view(conn: AsyncConnection, id_: UUID) -> dict[str, Any] | None:
    views = await get_ingestion_views(conn, [id_])
    return views[0] if views else None


def encode_cursor(received_at: datetime, id_: UUID) -> str:
    raw = json.dumps({"t": received_at.isoformat(), "i": str(id_)}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    return datetime.fromisoformat(data["t"]), UUID(data["i"])


async def list_ingestions(
    conn: AsyncConnection,
    *,
    status: str | None,
    device_imei: str | None,
    limit: int,
    cursor: tuple[datetime, UUID] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["TRUE"]
    params: dict[str, Any] = {"lim": limit + 1}
    if status:
        where.append("status = :status")
        params["status"] = status
    if device_imei:
        where.append("device_imei = :imei")
        params["imei"] = device_imei
    if cursor:
        where.append("(received_at, id) < (:ct, :ci)")
        params["ct"], params["ci"] = cursor
    rows = (
        await conn.execute(
            sa.text(
                f"SELECT {_VIEW_COLUMNS} FROM ingestions WHERE {' AND '.join(where)} "
                "ORDER BY received_at DESC, id DESC LIMIT :lim"
            ),
            params,
        )
    ).all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].received_at, rows[-1].id)
    return await _build_views(conn, list(rows)), next_cursor
