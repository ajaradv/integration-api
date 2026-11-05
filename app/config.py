from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Accept a libpq-style URL (as Neon's console shows it) and return an asyncpg one.

    postgres:// and postgresql:// become postgresql+asyncpg://, `sslmode` becomes
    asyncpg's `ssl`, and `channel_binding` (unsupported by asyncpg) is dropped.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        query.append(("ssl" if key == "sslmode" else key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:55432/paxafe"
    max_body_bytes: int = 262144
    log_level: str = "INFO"
    claim_stale_s: int = 300
    max_attempts: int = 5
    worker_max_jobs: int = 4
    worker_poll_min_s: float = 0.5
    worker_poll_max_s: float = 5.0
    job_timeout_s: float = 30.0
    heartbeat_interval_s: float = 10.0
    heartbeat_stale_s: float = 60.0
    livez_grace_s: float = 90.0

    @field_validator("database_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_database_url(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
