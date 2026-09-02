"""Gateway configuration — environment-driven (12-factor)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs are ``TALLY_``-prefixed env vars. Defaults target the docker-compose stack."""

    model_config = SettingsConfigDict(env_prefix="TALLY_", env_file=".env", extra="ignore")

    # Root log level (CTO-218). The gateway never configured logging, so under uvicorn the root
    # logger kept its WARNING-only lastResort and every gateway logger.info (startup line, ingest
    # buffer enablement, scheduler per-tick summary) was dropped in a compose deployment. Logging is
    # now configured once at boot at this level; raise to WARNING to quiet a noisy deployment or drop
    # to DEBUG to trace one. See gateway.app._configure_logging.
    log_level: str = "INFO"

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

    # Per-org HMAC key-material provider (Initiative 2, §3.2). Selects HOW the provisioner mints, and
    # the /v1/tenant/hmac-key bootstrap reads back, a tenant's active HMAC key set:
    #   * ``local`` (default): dev provider with NO cloud dependency. The material is DERIVED
    #     deterministically from the durable reference stored in ``tenants.hash_salt_kek_ref`` and the
    #     root secret below, so a tenant provisioned before a gateway restart still resolves to the
    #     same bytes afterwards (the old in-memory-only provider 404'd the hmac-key after a restart).
    #   * ``kms`` / ``secret-manager``: the production seam, backed by KMS / Secret Manager. Selectable
    #     here so a deployment can turn it on; the concrete client is wired by the deployment (the raw
    #     key set is never persisted to Postgres, only its reference is).
    hmac_key_provider: str = "local"
    # Dev-only root secret the ``local`` provider derives per-tenant HMAC material from. Deterministic
    # across restarts (that is the point), so it must be overridden in any shared/staging environment
    # and is NEVER used by the ``kms`` provider. Not a production key.
    hmac_local_root_secret: str = "tally-local-dev-hmac-root-secret-do-not-use-in-prod"

    # Control-plane service token (Initiative 1, §6). The web server is the ONLY legitimate caller of
    # the control-plane endpoints (`/v1/tenant/*`); it authenticates with this server-only shared
    # secret as `Authorization: Bearer <token>` and passes the resolved tenant UUID in `x-tenant-id`.
    # The token authenticates the WEB SERVER, not the tenant, and never rides in a client bundle. The
    # gate is active only when auth is on (``require_api_key``) AND a token is configured, so local
    # dev with auth off is unaffected. Empty means "no service-token gate" (the dev default).
    gateway_service_token: str = ""

    # Idempotency window (seconds) for (tenant_id, batch_id) dedup.
    idempotency_ttl_s: int = 24 * 3600

    # Edge-key delta feed safe-lag window, in seconds (Initiative 2 §6.2 review). The /v1/edge/keys
    # cursor is a keyset watermark over (GREATEST(created_at, revoked_at), id). created_at/revoked_at
    # are stamped at statement time, not commit time, so a slow transaction can commit a row whose
    # watermark is BELOW a cursor a later-but-faster commit already advanced past, and that row would
    # be skipped forever. The feed therefore refuses to advance the cursor past `now() - this margin`,
    # so any transaction that commits within the margin is still picked up on a later poll. Size it
    # above the longest expected api_keys write transaction plus app<->DB clock skew; a few seconds is
    # ample for single-row inserts/revokes. The cost is that a brand-new key takes up to this long to
    # appear in the feed, which is well within the proxy's refresh budget.
    edge_key_safe_lag_seconds: float = 5.0

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

    # Replay blob-store backend (CTO-152 / CTO-158). ``memory`` (default) uses the in-process dict
    # store; ``gcs`` persists scrubbed replay samples to a Google Cloud Storage bucket via ADC /
    # Workload Identity; ``s3`` persists them to an AWS S3 (or S3-compatible, e.g. MinIO) bucket via
    # the AWS default credential chain (IAM role / IRSA / instance profile / env) — no raw keys for
    # either. ``replay_gcs_bucket`` is required when the backend is ``gcs``; ``replay_s3_bucket`` is
    # required when the backend is ``s3``.
    replay_blob_backend: str = "memory"
    replay_gcs_bucket: str = ""
    # S3 backend (CTO-158). Bucket is required for ``s3``; prefix/region are optional. Region empty
    # means "resolve from the AWS default chain" (``AWS_REGION`` / config / instance metadata).
    replay_s3_bucket: str = ""
    replay_s3_prefix: str = ""
    replay_s3_region: str = ""

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

    # GCP Cloud Billing compute connector (CTO-150). The compute cost layer's GCP source reads a
    # tenant's Cloud Billing BigQuery *export* table (GCP has no fine-grained REST cost API — the BQ
    # export is the source of truth) and lands one synthetic `compute` span/day. A tenant opts in via
    # a `tenant_compute_config` row with cloud_provider='gcp'; these settings only tune the live BQ
    # job. Auth is via ADC / Workload Identity / a per-tenant Secret Manager reference — never a raw
    # service-account key. Requires the optional `[bigquery]` extra (google-cloud-bigquery),
    # lazy-imported in gateway.connectors.compute so the gateway boots without it.
    compute_gcp_enabled: bool = False
    # GCP project that runs the BigQuery billing-export jobs. Empty means "resolve from ADC".
    compute_gcp_bq_project: str = ""
    # Fallback export table (`project.dataset.gcp_billing_export_v1_XXXX`) for a tenant whose
    # tenant_compute_config row leaves bq_billing_export_table blank. Empty means "require per-tenant".
    compute_gcp_default_billing_export_table: str = ""
    # Vercel compute + egress cost connector (CTO-163). Pulls a tenant's Vercel usage/billing
    # (Functions compute + bandwidth) into the compute + egress cost layers. DISABLED by default at
    # both the process level (this flag) and per-tenant (``tenant_vercel_config.enabled``), so no
    # existing deployment starts pulling on upgrade. Auth is via a Vercel access token stored BY
    # REFERENCE per-tenant (``tenant_vercel_config.access_token_ref``, a Secret Manager pointer) —
    # never a raw token in config, the DB, or logs. Requires the optional ``requests`` dep, imported
    # lazily in :mod:`gateway.connectors.vercel` so the gateway boots without it.
    vercel_connector_enabled: bool = False
    # Vercel API base (override for a proxy / test double). No credentials in the URL.
    vercel_api_base: str = "https://api.vercel.com"
    # Egress double-count reconciliation with CTO-144 (default off): when false, Vercel egress flows
    # solely through the CTO-144 egress connector and this connector emits ONLY compute spans. A
    # per-tenant ``tenant_vercel_config.emit_egress`` flag can opt a tenant into egress here instead —
    # set it ONLY when the tenant has no CTO-144 Vercel egress row. This process flag is the default
    # for tenants that leave the column NULL.
    vercel_emit_egress: bool = False
    # S3 / Athena (+ Redshift) export sink (CTO-160). The AWS analog of the BigQuery export above:
    # an OPTIONAL, additive analytics mirror that copies the SAME reconciled facts (spans /
    # business_events / attribution / daily rollups, via the shared ExportTableSpec set) into a
    # tenant's OWN S3 bucket as partitioned Parquet, queryable through Athena / Glue and loadable
    # into Redshift. ClickHouse stays the primary store and the dashboard keeps reading it — this
    # is a mirror, never a replacement. DISABLED by default at both the process level (this flag)
    # and per-tenant (``tenant_athena_export_config.enabled``, also default false), so no existing
    # deployment starts exporting on upgrade. Auth is via an assumed IAM role / instance profile /
    # IRSA referenced per-tenant — never a raw AWS access key. Requires the optional ``[athena]``
    # extra (``pyarrow`` + ``boto3``), lazy-imported in :mod:`gateway.athena_export` so the gateway
    # boots without it.
    athena_export_enabled: bool = False
    # S3 bucket owning the export prefixes. Empty means "resolve per-tenant".
    athena_export_s3_bucket: str = ""
    # Defaults for a tenant that enables export without overriding prefix/database/region.
    athena_export_default_prefix: str = "tally/"
    athena_export_default_database: str = "tally_export"
    athena_export_aws_region: str = ""
    # Max rows pulled from ClickHouse per (tenant, table) export pass.
    athena_export_batch_limit: int = 10_000

    # Scheduler (CTO-213). Periodic per-tenant job execution, started from the FastAPI lifespan the
    # same way the ingest buffer is. OFF by default like every other background feature here, so an
    # upgrade changes nothing: with the flag off no task is created and behaviour is identical to
    # today. CTO-213 registers no jobs at all (the cost connectors are CTO-215, the ingest workers
    # CTO-216), so even enabling it ticks over an empty registry until one of those lands.
    #
    # Safe on more than one replica since CTO-214: every (job, tenant) pair is guarded by a
    # Postgres advisory lock, and a replica that cannot take the lock leaves that tenant to the one
    # that can and re-asks next tick. No flag of its own, because the alternative is the
    # double-counted spend it exists to prevent. See gateway/scheduler.py.
    scheduler_enabled: bool = False
    # How often the loop wakes to ask "is anything due". This is NOT a job's cadence — cadence is
    # per job and is measured against recorded run history, so this only bounds how late a due job
    # can be picked up. Minutes, because the finest useful cadence in this product is minutes.
    scheduler_tick_interval_s: float = 300.0
    # Backoff after repeated failure, so a permanently broken job is not retried every tick forever.
    # The first failure is not delayed at all; the second waits base, and it doubles to the cap.
    scheduler_backoff_base_s: float = 300.0
    scheduler_backoff_cap_s: float = 6 * 3600.0
    # CTO-219. How long shutdown WAITS for an in-flight tick before proceeding without it. It bounds
    # the wait, not the job: a job runs on a worker thread that cannot be cancelled, so past this
    # point the thread is left to die with the process and its run is not recorded (the next tick
    # sees the pair as still due). Without a bound, one connector talking to a slow billing API held
    # the whole deploy open, and with it the ingest buffer's flush. Long enough that a healthy tick
    # finishes normally, short enough that a rolling deploy never waits on one tenant.
    scheduler_shutdown_timeout_s: float = 30.0
    # CTO-219. Idle Postgres sessions the scheduler keeps warm for its state reads, run inserts and
    # tenant listing, which previously opened one connection per (job, tenant) per tick. Advisory
    # locks are NOT pooled: each held lock keeps its own dedicated session, because the session is
    # what makes the lock die with the process. See gateway/scheduler.py.
    scheduler_db_pool_max_idle: int = 4


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
