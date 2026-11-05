from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
