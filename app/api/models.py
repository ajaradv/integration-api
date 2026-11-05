from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WebhookAccepted(BaseModel):
    ingestion_id: UUID
    status: str
    status_url: str
    duplicate: bool = False


class ReadingView(BaseModel):
    kind: Literal["sensor", "location"]
    px_payload: dict[str, Any]
    current: bool
    superseded_by: UUID | None = None


class IngestionView(BaseModel):
    id: UUID
    status: str
    received_at: datetime
    processed_at: datetime | None = None
    attempts: int = 0
    issues: list[dict[str, Any]] = Field(default_factory=list)
    issues_history: list[dict[str, Any]] = Field(default_factory=list)
    readings: list[ReadingView] = Field(default_factory=list)
    candidate_sensor: dict[str, Any] | None = None
    candidate_location: dict[str, Any] | None = None
    write_result: dict[str, Any] | None = None
    probe_temperature: float | None = None
    probe_fault: bool | None = None
    promoted_at: datetime | None = None
    reprocess_count: int = 0


class IngestionPage(BaseModel):
    items: list[IngestionView]
    next_cursor: str | None = None


class IngestionBatch(BaseModel):
    items: list[IngestionView]


class Overrides(BaseModel):
    """Every field is optional. `temperature` may be an explicit null (see to_dict)."""

    model_config = ConfigDict(extra="forbid")

    device_imei: str | None = Field(default=None, pattern=r"^[0-9]{15}$")
    device_id: str | None = Field(default=None, min_length=1, max_length=64)
    timestamp: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    temperature: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Only the provided fields. An explicit `temperature: null` is kept (it nulls it);
        for every other field a null means "not provided"."""
        out: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            if value is not None or name == "temperature":
                out[name] = value
        return out


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: Overrides = Field(default_factory=Overrides)
    note: str = Field(min_length=1, max_length=500)
    replace_existing: bool = False


class HealthView(BaseModel):
    database: Literal["ok", "unreachable"]
    worker_heartbeat_age_s: float | None
    pending: int | None
    processing: int | None
    failed: int | None


class LivezView(BaseModel):
    status: Literal["ok", "unavailable"]
    worker: Literal["ok", "stale", "unknown", "starting"]
