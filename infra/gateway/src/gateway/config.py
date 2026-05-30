"""Gateway configuration — environment-driven (12-factor)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs are ``TALLY_``-prefixed env vars. Defaults target the docker-compose stack."""

    model_config = SettingsConfigDict(env_prefix="TALLY_", env_file=".env", extra="ignore")

    # ClickHouse (HTTP interface — clickhouse-connect).
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_db: str = "tally"
    clickhouse_user: str = "tally"
    clickhouse_password: str = "tally"

    # Postgres (control plane) — used for API-key auth lookups.
    postgres_dsn: str = "postgresql://tally:tally@localhost:5432/tally"

    # Auth. When false, the gateway trusts the batch's tenant_id (local dev). When true, requests
    # must carry `Authorization: Bearer <key>` whose SHA-256 is registered in api_keys.
    require_api_key: bool = False

    # Idempotency window (seconds) for (tenant_id, batch_id) dedup.
    idempotency_ttl_s: int = 24 * 3600


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
