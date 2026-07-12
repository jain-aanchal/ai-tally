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

    # Per-tenant rate limit (token bucket) + monthly span quota (CTO-33). Process-local enforcement;
    # cluster-wide fairness is a later concern (CTO-30). Defaults are generous for local dev.
    rate_limit_rps: float = 500.0
    rate_limit_burst: float = 2000.0
    monthly_quota_spans: int = 50_000_000

    # Per-span payload cap (bytes) for boundary validation (CTO-34).
    max_span_bytes: int = 64 * 1024

    # Backpressure (CTO-36): concurrent in-flight ingest requests at/above which the gateway
    # tightens client flow-control hints and sheds the overflow of a batch as retryable.
    backpressure_soft_limit: int = 64

    # Ingest burst buffer (CTO-37). When enabled, accepted span rows are enqueued to an in-memory
    # burst buffer (Kafka in prod) and written to ClickHouse by a background drain loop, so a burst
    # or a briefly-slow ClickHouse never makes the gateway return 5xx on the hot path. Off by default
    # to preserve the synchronous write path; flip on per-deployment.
    ingest_buffered: bool = False
    # High-water mark (rows) past which the buffer sheds overflow as retryable (never 5xx).
    ingest_buffer_capacity: int = 200_000
    # Rows drained to ClickHouse per cycle, and idle poll interval between drains.
    ingest_buffer_drain_batch: int = 2_000
    ingest_buffer_poll_interval_s: float = 0.05

    # Replay blob-store backend (CTO-152). ``memory`` (default) uses the in-process dict store;
    # ``gcs`` persists scrubbed replay samples to a Google Cloud Storage bucket via ADC / Workload
    # Identity (no raw key material). ``replay_gcs_bucket`` is required when the backend is ``gcs``.
    replay_blob_backend: str = "memory"
    replay_gcs_bucket: str = ""

    # BigQuery export sink (CTO-154). OPTIONAL, additive analytics mirror that copies
    # spans / business_events / attribution / daily rollups into a tenant's OWN BigQuery
    # dataset. ClickHouse stays the primary store and the dashboard keeps reading it — this
    # is a mirror, never a replacement. DISABLED by default at both the process level (this
    # flag) and per-tenant (``tenant_bq_export_config.enabled``, also default false), so no
    # existing deployment starts exporting on upgrade. Auth is via ADC / Workload Identity /
    # a Secret Manager reference stored per-tenant — never a raw service-account key.
    # Requires the optional ``[bigquery]`` extra (``google-cloud-bigquery``), lazy-imported
    # in :mod:`gateway.bq_export` so the gateway boots without it.
    bq_export_enabled: bool = False
    # GCP project that owns the export datasets. Empty means "resolve from ADC / per-tenant".
    bq_export_gcp_project: str = ""
    # Defaults for a tenant that enables export without overriding dataset/prefix.
    bq_export_default_dataset: str = "tally_export"
    bq_export_default_table_prefix: str = "tally_"
    # Max rows pulled from ClickHouse per (tenant, table) export pass.
    bq_export_batch_limit: int = 10_000


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
