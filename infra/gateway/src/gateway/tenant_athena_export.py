"""Per-tenant config + watermark stores for the S3 / Athena export sink (CTO-160).

The AWS analog of ``tenant_bq_export`` (CTO-154). Exporting a tenant's telemetry to their own S3
bucket as partitioned Parquet is opt-in and off by default: a tenant with no row returns
:data:`DEFAULT_CONFIG` (``enabled=False``), so no existing deployment starts mirroring on upgrade.
When enabled, the tenant supplies their destination (``bucket`` / ``prefix`` / ``database`` + AWS
``region``) and a **credential reference** — an IAM **role ARN** the worker assumes, or a Secrets
Manager / SSM parameter resource name. We deliberately reject anything that looks like an inline AWS
access key / secret: raw keys never live in this table.

``AthenaExportWatermarkStore`` is the incremental cursor, one row per (tenant, source table),
mirroring the ``bq_export_watermarks`` / ``reconciliation_runs`` pattern, and implements the shared
:class:`gateway.bq_export.WatermarkStore` protocol so it drops straight into ``run_export``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from gateway.config import Settings


class RawKeyRejected(ValueError):
    """Raised when a credential ref looks like an inline AWS key rather than a reference."""


# Substrings that betray an inline/raw AWS access key or secret being pasted where only a *reference*
# (an IAM role ARN or a Secrets Manager / SSM resource name) belongs. Auth must flow through an
# assumed role / instance profile / IRSA, never a raw key.
_RAW_KEY_MARKERS = (
    "aws_secret_access_key",
    "aws_access_key_id",
    "aws_session_token",
    "-----begin",
    "akia",  # long-lived AWS access key id prefix
    "asia",  # temporary AWS access key id prefix
)


def _reject_raw_key(credential_ref: str) -> None:
    lowered = credential_ref.lower()
    if any(marker in lowered for marker in _RAW_KEY_MARKERS):
        raise RawKeyRejected(
            "credential_ref must be a reference (an IAM role ARN, or a Secrets Manager / SSM "
            "resource name) — never an inline AWS access key or secret."
        )


@dataclass(frozen=True, slots=True)
class AthenaExportConfig:
    enabled: bool
    bucket: str
    prefix: str
    database: str
    region: str
    # IAM role ARN to assume, or a Secrets Manager / SSM resource name. Never a raw key.
    credential_ref: str

    def dataset_ref(self) -> str:
        """The S3 key prefix a table name appends to — always trailing-slash-terminated."""
        return self.prefix if self.prefix.endswith("/") else self.prefix + "/"

    def s3_uri(self, table: str) -> str:
        """The ``s3://`` root for one table's partitioned Parquet."""
        return f"s3://{self.bucket}/{self.dataset_ref()}{table}/"

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "database": self.database,
            "region": self.region,
            "credential_ref": self.credential_ref,
        }


def default_config(settings: Settings) -> AthenaExportConfig:
    return AthenaExportConfig(
        enabled=False,
        bucket=settings.athena_export_s3_bucket,
        prefix=settings.athena_export_default_prefix,
        database=settings.athena_export_default_database,
        region=settings.athena_export_aws_region,
        credential_ref="",
    )


# Module-level default with no Settings (off, empty destination). Convenience for callers/tests.
DEFAULT_CONFIG = AthenaExportConfig(
    enabled=False, bucket="", prefix="tally/", database="tally_export", region="", credential_ref=""
)


class TenantAthenaExportStore:
    """Postgres surface over ``tenant_athena_export_config`` (mirrors ``TenantBQExportStore``)."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn
        self._defaults = default_config(settings)

    def get(self, tenant_id: str) -> AthenaExportConfig:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled, bucket, prefix, database, region, credential_ref
                FROM tenant_athena_export_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return self._defaults
            return AthenaExportConfig(
                enabled=bool(row[0]),
                bucket=str(row[1]),
                prefix=str(row[2]),
                database=str(row[3]),
                region=str(row[4]),
                credential_ref=str(row[5]),
            )

    def upsert(
        self,
        tenant_id: str,
        *,
        enabled: bool | None = None,
        bucket: str | None = None,
        prefix: str | None = None,
        database: str | None = None,
        region: str | None = None,
        credential_ref: str | None = None,
    ) -> AthenaExportConfig:
        current = self.get(tenant_id)
        new = AthenaExportConfig(
            enabled=current.enabled if enabled is None else bool(enabled),
            bucket=current.bucket if bucket is None else str(bucket),
            prefix=current.prefix if prefix is None else str(prefix),
            database=current.database if database is None else str(database),
            region=current.region if region is None else str(region),
            credential_ref=current.credential_ref
            if credential_ref is None
            else str(credential_ref),
        )
        if new.credential_ref:
            _reject_raw_key(new.credential_ref)
        if new.enabled and not (new.bucket and new.database):
            raise ValueError("bucket and database are required when export is enabled")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_athena_export_config
                    (tenant_id, enabled, bucket, prefix, database, region, credential_ref,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (tenant_id) DO UPDATE
                  SET enabled        = EXCLUDED.enabled,
                      bucket         = EXCLUDED.bucket,
                      prefix         = EXCLUDED.prefix,
                      database       = EXCLUDED.database,
                      region         = EXCLUDED.region,
                      credential_ref = EXCLUDED.credential_ref,
                      updated_at     = now()
                """,
                (
                    tenant_id,
                    new.enabled,
                    new.bucket,
                    new.prefix,
                    new.database,
                    new.region,
                    new.credential_ref,
                ),
            )
            conn.commit()
        return new


class AthenaExportWatermarkStore:
    """Postgres-backed incremental cursor: one row per (tenant, source table).

    Implements the :class:`gateway.bq_export.WatermarkStore` protocol so it drops straight into
    ``run_export``. A missing row means "never exported" and reads back as ``None`` — a full
    initial backfill on first run.
    """

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn

    def get(self, tenant_id: str, source_table: str) -> datetime | date | None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT watermark
                FROM athena_export_watermarks
                WHERE tenant_id = %s AND source_table = %s
                """,
                (tenant_id, source_table),
            )
            row = cur.fetchone()
            return row[0] if row is not None else None

    def advance(
        self, tenant_id: str, source_table: str, watermark: datetime | date, rows: int
    ) -> None:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO athena_export_watermarks
                    (tenant_id, source_table, watermark, rows_exported, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (tenant_id, source_table) DO UPDATE
                  SET watermark     = EXCLUDED.watermark,
                      rows_exported = athena_export_watermarks.rows_exported
                                        + EXCLUDED.rows_exported,
                      updated_at    = now()
                """,
                (tenant_id, source_table, watermark, rows),
            )
            conn.commit()
