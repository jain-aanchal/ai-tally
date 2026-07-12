"""Per-tenant config + watermark stores for the BigQuery export sink (CTO-154).

Exporting a tenant's telemetry into their own BigQuery dataset is opt-in and off by default: a
tenant with no row returns :data:`DEFAULT_CONFIG` (``enabled=False``), so no existing deployment
starts mirroring on upgrade. When enabled, the tenant supplies their destination
(``project_id`` / ``dataset`` / ``table_prefix``) and a **credential reference** — an ADC hint,
a Workload-Identity service-account email, or a Secret Manager resource name. We deliberately
reject anything that looks like an inline service-account key: raw keys never live in this table.

``BQExportWatermarkStore`` is the append-per-pass incremental cursor, one row per
(tenant, source table), mirroring the ``reconciliation_runs`` run-log pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from gateway.config import Settings


class RawKeyRejected(ValueError):
    """Raised when a credential ref looks like an inline key rather than a reference."""


# Substrings that betray an inline/raw service-account key being pasted where only a *reference*
# belongs. Auth must flow through ADC / Workload Identity / Secret Manager, never a raw key.
_RAW_KEY_MARKERS = ("private_key", "-----begin", "\"type\": \"service_account\"", "'type':")


def _reject_raw_key(credential_ref: str) -> None:
    lowered = credential_ref.lower()
    if any(marker in lowered for marker in _RAW_KEY_MARKERS):
        raise RawKeyRejected(
            "credential_ref must be a reference (ADC hint, Workload-Identity SA email, or "
            "Secret Manager resource name) — never an inline service-account key."
        )


@dataclass(frozen=True, slots=True)
class BQExportConfig:
    enabled: bool
    project_id: str
    dataset: str
    table_prefix: str
    # ADC hint / Workload-Identity SA email / Secret Manager resource name. Never a raw key.
    credential_ref: str

    def dataset_ref(self) -> str:
        """Fully-qualified ``project.dataset.prefix`` a table name appends to."""
        return f"{self.project_id}.{self.dataset}.{self.table_prefix}"

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "project_id": self.project_id,
            "dataset": self.dataset,
            "table_prefix": self.table_prefix,
            "credential_ref": self.credential_ref,
        }


def default_config(settings: Settings) -> BQExportConfig:
    return BQExportConfig(
        enabled=False,
        project_id=settings.bq_export_gcp_project,
        dataset=settings.bq_export_default_dataset,
        table_prefix=settings.bq_export_default_table_prefix,
        credential_ref="",
    )


# Module-level default with no Settings (off, empty destination). Convenience for callers/tests.
DEFAULT_CONFIG = BQExportConfig(
    enabled=False, project_id="", dataset="tally_export", table_prefix="tally_", credential_ref=""
)


class TenantBQExportStore:
    """Tiny Postgres surface over ``tenant_bq_export_config`` (mirrors ``TenantReplayStore``)."""

    def __init__(self, settings: Settings) -> None:
        self._dsn = settings.postgres_dsn
        self._defaults = default_config(settings)

    def get(self, tenant_id: str) -> BQExportConfig:
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled, project_id, dataset, table_prefix, credential_ref
                FROM tenant_bq_export_config
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return self._defaults
            return BQExportConfig(
                enabled=bool(row[0]),
                project_id=str(row[1]),
                dataset=str(row[2]),
                table_prefix=str(row[3]),
                credential_ref=str(row[4]),
            )

    def upsert(
        self,
        tenant_id: str,
        *,
        enabled: bool | None = None,
        project_id: str | None = None,
        dataset: str | None = None,
        table_prefix: str | None = None,
        credential_ref: str | None = None,
    ) -> BQExportConfig:
        current = self.get(tenant_id)
        new = BQExportConfig(
            enabled=current.enabled if enabled is None else bool(enabled),
            project_id=current.project_id if project_id is None else str(project_id),
            dataset=current.dataset if dataset is None else str(dataset),
            table_prefix=current.table_prefix if table_prefix is None else str(table_prefix),
            credential_ref=current.credential_ref
            if credential_ref is None
            else str(credential_ref),
        )
        if new.credential_ref:
            _reject_raw_key(new.credential_ref)
        if new.enabled and not (new.project_id and new.dataset):
            raise ValueError("project_id and dataset are required when export is enabled")
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_bq_export_config
                    (tenant_id, enabled, project_id, dataset, table_prefix, credential_ref,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (tenant_id) DO UPDATE
                  SET enabled        = EXCLUDED.enabled,
                      project_id     = EXCLUDED.project_id,
                      dataset        = EXCLUDED.dataset,
                      table_prefix   = EXCLUDED.table_prefix,
                      credential_ref = EXCLUDED.credential_ref,
                      updated_at     = now()
                """,
                (
                    tenant_id,
                    new.enabled,
                    new.project_id,
                    new.dataset,
                    new.table_prefix,
                    new.credential_ref,
                ),
            )
            conn.commit()
        return new


class BQExportWatermarkStore:
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
                FROM bq_export_watermarks
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
                INSERT INTO bq_export_watermarks
                    (tenant_id, source_table, watermark, rows_exported, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (tenant_id, source_table) DO UPDATE
                  SET watermark     = EXCLUDED.watermark,
                      rows_exported = bq_export_watermarks.rows_exported + EXCLUDED.rows_exported,
                      updated_at    = now()
                """,
                (tenant_id, source_table, watermark, rows),
            )
            conn.commit()
