"""Database access: engine factory, request connection dependency, Core tables."""

from __future__ import annotations

from collections.abc import AsyncIterator

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import Settings

metadata = sa.MetaData()

_now = sa.text("now()")
_uuid = sa.text("gen_random_uuid()")

api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=_uuid),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("key_hash", sa.CHAR(64), nullable=False, unique=True),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column("last_used_at", sa.DateTime(timezone=True)),
)

ingestions = sa.Table(
    "ingestions",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=_uuid),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
    sa.Column("api_key_id", UUID(as_uuid=True), sa.ForeignKey("api_keys.id"), nullable=False),
    sa.Column("body_hash", sa.CHAR(64), nullable=False),
    sa.Column("idempotency_key", sa.String(255)),
    sa.Column("raw_body", BYTEA, nullable=False),
    sa.Column("raw_json", JSONB),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
    sa.Column("claimed_at", sa.DateTime(timezone=True)),
    sa.Column("processed_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("issues", JSONB),
    sa.Column("issues_history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("candidate_sensor", JSONB),
    sa.Column("candidate_location", JSONB),
    sa.Column("write_result", JSONB),
    sa.Column("account_id", sa.BigInteger),
    sa.Column("device_imei", sa.Text),
    sa.Column("reading_timestamp_ms", sa.BigInteger),
    sa.Column("promoted_by", UUID(as_uuid=True), sa.ForeignKey("api_keys.id")),
    sa.Column("promoted_at", sa.DateTime(timezone=True)),
    sa.Column("promotion_overrides", JSONB),
    sa.Column("reprocess_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("reprocessed_by", UUID(as_uuid=True), sa.ForeignKey("api_keys.id")),
    sa.Column("reprocessed_at", sa.DateTime(timezone=True)),
)

worker_heartbeats = sa.Table(
    "worker_heartbeats",
    metadata,
    sa.Column("worker_id", sa.Text, primary_key=True),
    sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
)


def _reading_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("px_sensor", JSONB, nullable=False),
        sa.Column("px_location", JSONB, nullable=False),
        sa.Column("device_imei", sa.Text),
        sa.Column("timestamp_ms", sa.BigInteger),
        sa.Column("temperature", sa.Numeric),
        sa.Column("latitude", sa.Float),
        sa.Column("longitude", sa.Float),
        sa.Column("probe_temperature", sa.Numeric),
        sa.Column("probe_fault", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("account_id", sa.BigInteger),
        sa.Column("entity_name", sa.Text),
        sa.Column("shipment_id", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
    ]


# device_imei, timestamp_ms, temperature, latitude and longitude are GENERATED ALWAYS columns
# in the database (see migrations/versions/0001_initial.py); the application never writes them.
readings = sa.Table(
    "readings",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("ingestion_id", UUID(as_uuid=True), sa.ForeignKey("ingestions.id"), nullable=False),
    *_reading_columns(),
)

readings_history = sa.Table(
    "readings_history",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("reading_id", sa.BigInteger, nullable=False),
    sa.Column("ingestion_id", UUID(as_uuid=True), nullable=False),
    *_reading_columns(),
    sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=False, server_default=_now),
    sa.Column("replaced_by_ingestion_id", UUID(as_uuid=True), nullable=False),
)


def create_engine(settings: Settings, pool_size: int = 5) -> AsyncEngine:
    """Neon suspends idle compute; pool_pre_ping recovers the pool after it wakes."""
    return create_async_engine(
        settings.database_url,
        pool_size=pool_size,
        max_overflow=0,
        pool_pre_ping=True,
    )


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One connection per request. Routes that write commit explicitly."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        yield conn
