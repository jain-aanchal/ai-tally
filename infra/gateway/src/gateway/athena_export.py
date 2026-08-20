"""Optional S3 / Athena (+ Redshift) analytics export sink (CTO-160).

The AWS analog of the BigQuery export (CTO-154). An **additive, opt-in mirror** of a tenant's
telemetry into their *own* S3 bucket as partitioned Parquet, queryable through an Athena external
table (or a Glue catalog entry) and loadable into Redshift with a ``COPY`` recipe. ClickHouse stays
ai-tally's primary store and the dashboard keeps reading it; this worker only copies rows out. It is
never a replacement and never reads back into the product.

By design this module **reuses CTO-154's machinery so both sinks emit identical facts**:

* the same :class:`~gateway.bq_export.ExportTableSpec` set (:data:`~gateway.bq_export.ALL_SPECS`) —
  spans / business_events / attribution / daily rollups, the same columns, the same natural keys and
  the same incremental watermark columns;
* the same :class:`~gateway.bq_export.ClickHouseExportReader` (``client.query(sql).result_rows``),
  the same :func:`~gateway.bq_export.project_row` / :func:`~gateway.bq_export.strip_body_keys`
  body-stripping, and the same :class:`~gateway.bq_export.WatermarkStore` protocol;
* the same no-bodies invariant, re-asserted at import time here against the shared specs.

What differs is only the **sink**: instead of an upsert-by-key ``MERGE`` into BigQuery, rows are
written to ``s3://<bucket>/<prefix>/<table>/dt=YYYY-MM-DD/data.parquet`` as Parquet, one object per
day partition. Idempotency is **per day partition**: a partition write upserts the partition's rows
on the same natural key ClickHouse dedupes on (read-merge-write), so replaying a pass — or picking up
more rows for a day already partially exported — never duplicates and never drops earlier rows.

**Optional, lazy-imported deps.** ``pyarrow`` (Parquet encode) and ``boto3`` (S3 put) live behind the
``[athena]`` extra and are imported *inside* the real sink only, so this module imports and the
gateway boots without them (exactly like ``bq_export``'s ``[bigquery]`` extra).

**No bodies, by construction (CTO-118 / CTO-125).** Because the specs are shared with CTO-154, the
same guards apply: no exported column is body-keyed, no spec sources a replay table, and the
long-tail ``SpanAttributes`` / ``RawPayload`` JSON maps are body-stripped again before they leave.
The replay candidate-response text (CTO-125) is a separate opt-in tier and is excluded here too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol

# Reuse CTO-154's schema + reader + body-strip so both sinks emit identical facts.
from gateway.bq_export import (
    ALL_SPECS,
    SPECS_BY_TABLE,
    BQField,
    ClickHouseExportReader,  # noqa: F401 - re-exported for the runbook wiring
    ExportReader,  # noqa: F401 - re-exported: the reader protocol is shared
    ExportTableSpec,
    WatermarkStore,  # noqa: F401 - re-exported: the watermark protocol is shared
    _EXCLUDED_SOURCE_TABLES,
    _is_body_key,
    project_row,
    strip_body_keys,  # noqa: F401 - re-exported for callers/tests
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence


class AthenaDependencyMissing(RuntimeError):
    """Raised when export is invoked but the optional ``[athena]`` extra isn't installed."""


# --- No-bodies invariant, re-asserted at import against the shared specs ------------------------

def _assert_no_body_columns() -> None:
    """Fail fast if any shared spec would export a body-keyed column or read a replay table."""
    for spec in ALL_SPECS:
        if spec.source_table in _EXCLUDED_SOURCE_TABLES:
            raise AssertionError(
                f"export spec {spec.name!r} sources an excluded body-bearing table "
                f"{spec.source_table!r}"
            )
        body_cols = [c for c in spec.columns if _is_body_key(c)]
        if body_cols:
            raise AssertionError(
                f"export spec {spec.name!r} would export body-keyed columns {body_cols!r}"
            )
        banned = {"responsetext", "response_text"}
        leaked = [c for c in spec.columns if c.lower() in banned]
        if leaked:
            raise AssertionError(f"export spec {spec.name!r} leaks replay body column {leaked!r}")


_assert_no_body_columns()


# --- Partitioning ------------------------------------------------------------------------------

# Hive-style day partition column laid over every table's S3 prefix, derived from that table's
# incremental watermark column. Athena reads it as a partition; Redshift Spectrum / COPY ignore it.
PARTITION_COLUMN = "dt"


def partition_key(spec: ExportTableSpec, row: dict[str, Any]) -> date:
    """The day partition a row lands in, from its watermark column (``date`` or ``datetime``)."""
    value = row.get(spec.watermark_column)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(
        f"row for {spec.source_table!r} has no usable {spec.watermark_column!r} to partition on"
    )


def partition_prefix(dataset_ref: str, spec: ExportTableSpec, day: date) -> str:
    """The S3 key prefix for one table's one-day partition: ``<prefix><table>/dt=YYYY-MM-DD/``."""
    return f"{dataset_ref}{spec.name}/{PARTITION_COLUMN}={day.isoformat()}/"


# --- Athena / Redshift DDL ---------------------------------------------------------------------

# BigQuery standard-SQL type -> Athena (Hive/Presto) type. Kept as a pure mapping so the DDL is
# generated from the *same* BQField schema the BigQuery sink uses — one schema, two dialects.
_ATHENA_TYPES: dict[str, str] = {
    "STRING": "string",
    "INT64": "bigint",
    "NUMERIC": "decimal(38, 9)",
    "FLOAT64": "double",
    "TIMESTAMP": "timestamp",
    "DATE": "date",
    # Athena has no native JSON storage type for a Parquet column; the map is stored as a JSON string.
    "JSON": "string",
}


def athena_type(field: BQField) -> str:
    """Map one shared :class:`BQField` to its Athena column type."""
    try:
        return _ATHENA_TYPES[field.type]
    except KeyError as exc:  # pragma: no cover - guards a future unmapped type
        raise ValueError(f"no Athena type mapping for BigQuery type {field.type!r}") from exc


def athena_create_table_ddl(spec: ExportTableSpec, *, database: str, s3_location: str) -> str:
    """``CREATE EXTERNAL TABLE`` over the partitioned Parquet prefix for one export table.

    ``s3_location`` is the table root (``s3://bucket/prefix/<table>/``); the day partition column
    ``dt`` is declared with ``PARTITIONED BY`` and is *not* repeated in the column list. After a
    load, new partitions are registered with ``MSCK REPAIR TABLE`` (or a Glue crawler).
    """
    cols = ",\n".join(f"  `{f.name}` {athena_type(f)}" for f in spec.schema)
    location = s3_location if s3_location.endswith("/") else s3_location + "/"
    return (
        f"CREATE EXTERNAL TABLE IF NOT EXISTS `{database}`.`{spec.name}` (\n"
        f"{cols}\n"
        f")\n"
        f"PARTITIONED BY (`{PARTITION_COLUMN}` date)\n"
        f"STORED AS PARQUET\n"
        f"LOCATION '{location}'\n"
        f"TBLPROPERTIES ('parquet.compression' = 'SNAPPY');"
    )


def redshift_copy_sql(spec: ExportTableSpec, *, s3_location: str, iam_role: str) -> str:
    """A Redshift ``COPY`` recipe loading the same Parquet into a Redshift table.

    ``iam_role`` is an IAM **role ARN reference** (never a raw key); Redshift assumes it to read S3.
    """
    location = s3_location if s3_location.endswith("/") else s3_location + "/"
    return (
        f"COPY {spec.name}\n"
        f"FROM '{location}'\n"
        f"IAM_ROLE '{iam_role}'\n"
        f"FORMAT AS PARQUET;"
    )


def all_ddl(*, database: str, bucket: str, prefix: str) -> dict[str, str]:
    """The ``CREATE EXTERNAL TABLE`` DDL for every shared spec, keyed by table name."""
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    return {
        spec.name: athena_create_table_ddl(
            spec, database=database, s3_location=f"s3://{bucket}/{prefix}{spec.name}/"
        )
        for spec in ALL_SPECS
    }


# --- Sink (Protocol + in-memory fake + lazy real client) ---------------------------------------

class S3ParquetSink(Protocol):
    """Minimal put-only contract: write one day partition's rows as Parquet, idempotently."""

    def write_partition(
        self,
        *,
        table: str,
        prefix: str,
        schema: Sequence[BQField],
        key_columns: Sequence[str],
        rows: Sequence[dict[str, Any]],
    ) -> int: ...


class InMemoryS3ParquetSink:
    """Dict-backed sink used by tests and local dev.

    Idempotent **per day partition** and by the same natural key ClickHouse dedupes on:
    ``write_partition`` merges rows into the partition keyed by the key tuple, so replaying a pass —
    or exporting more rows for a day already partially written — never duplicates and never drops
    earlier rows of that partition.
    """

    def __init__(self) -> None:
        # prefix -> {key tuple -> row}
        self._partitions: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
        self._writes = 0

    def write_partition(
        self,
        *,
        table: str,
        prefix: str,
        schema: Sequence[BQField],
        key_columns: Sequence[str],
        rows: Sequence[dict[str, Any]],
    ) -> int:
        partition = self._partitions.setdefault(prefix, {})
        for row in rows:
            key = tuple(row.get(k) for k in key_columns)
            partition[key] = dict(row)
        self._writes += 1
        return len(rows)

    def rows(self, prefix: str) -> list[dict[str, Any]]:
        return list(self._partitions.get(prefix, {}).values())

    def table_rows(self, table_prefix: str) -> list[dict[str, Any]]:
        """All rows across every partition of one table (prefix ``<dataset_ref><table>/``)."""
        out: list[dict[str, Any]] = []
        for prefix, part in self._partitions.items():
            if prefix.startswith(table_prefix):
                out.extend(part.values())
        return out

    @property
    def partitions(self) -> list[str]:
        return list(self._partitions)

    @property
    def write_count(self) -> int:
        return self._writes

    def __len__(self) -> int:
        return sum(len(p) for p in self._partitions.values())


class Boto3S3ParquetSink:
    """Real sink writing Parquet to S3 via ``pyarrow`` + ``boto3``. Built by :func:`load_s3_parquet_sink`.

    Idempotency is per day partition: the existing partition object (if any) is read back, merged with
    the incoming rows on the natural key, and re-uploaded as a single Parquet object. This mirrors the
    BigQuery sink's staging + ``MERGE`` upsert, at partition granularity, so a replayed pass is safe.

    NB: exercised only against live S3 — the unit suite drives :class:`InMemoryS3ParquetSink`. See
    docs/athena-export.md for the runbook.
    """

    _OBJECT_NAME = "data.parquet"

    def __init__(self, bucket: str, *, region: str | None = None) -> None:  # pragma: no cover
        import boto3  # lazy: optional [athena] dep

        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region or None)

    def write_partition(  # pragma: no cover - needs live S3
        self,
        *,
        table: str,
        prefix: str,
        schema: Sequence[BQField],
        key_columns: Sequence[str],
        rows: Sequence[dict[str, Any]],
    ) -> int:
        import io

        import pyarrow as pa  # lazy: optional [athena] dep
        import pyarrow.parquet as pq  # lazy: optional [athena] dep

        if not rows:
            return 0
        key = f"{prefix}{self._OBJECT_NAME}"

        # Read-merge-write: fold in any rows already in this partition, upserting on the natural key.
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        existing = self._get_object(key)
        if existing is not None:
            table_in = pq.read_table(io.BytesIO(existing))
            for rec in table_in.to_pylist():
                merged[tuple(rec.get(k) for k in key_columns)] = rec
        for row in rows:
            merged[tuple(row.get(k) for k in key_columns)] = dict(row)

        columns = [f.name for f in schema]
        records = list(merged.values())
        arrow_table = pa.Table.from_pylist([_normalize(r, columns) for r in records])
        buf = io.BytesIO()
        pq.write_table(arrow_table, buf, compression="snappy")
        self._client.put_object(Bucket=self._bucket, Key=key, Body=buf.getvalue())
        return len(rows)

    def _get_object(self, key: str) -> bytes | None:  # pragma: no cover - needs live S3
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            return None
        return resp["Body"].read()


def _normalize(row: dict[str, Any], columns: Sequence[str]) -> dict[str, Any]:  # pragma: no cover
    """Coerce a projected row to the declared column set (JSON maps -> JSON string for Parquet)."""
    import json

    out: dict[str, Any] = {}
    for col in columns:
        value = row.get(col)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), sort_keys=True)
        out[col] = value
    return out


def load_s3_parquet_sink(bucket: str, *, region: str | None = None) -> S3ParquetSink:
    """Lazily construct the real S3 sink; raises if the ``[athena]`` extra is absent.

    Auth is left to the default AWS credential chain (instance/role profile, IRSA, or an assumed
    role referenced per-tenant) — this function never takes a raw secret key.
    """
    try:
        import boto3  # noqa: F401, PLC0415 - lazy optional import by design
        import pyarrow  # noqa: F401, PLC0415 - lazy optional import by design
    except ImportError as exc:  # pragma: no cover - depends on env
        raise AthenaDependencyMissing(
            "S3/Athena export requires the optional 'athena' extra: "
            "`uv sync --extra athena` (installs pyarrow + boto3)."
        ) from exc
    return Boto3S3ParquetSink(bucket, region=region)


# --- Orchestrator ------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExportResult:
    source_table: str
    rows_exported: int
    partitions_written: int
    previous_watermark: datetime | date | None
    new_watermark: datetime | date | None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_table": self.source_table,
            "rows_exported": self.rows_exported,
            "partitions_written": self.partitions_written,
            "previous_watermark": _iso(self.previous_watermark),
            "new_watermark": _iso(self.new_watermark),
        }


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def export_table(
    *,
    spec: ExportTableSpec,
    reader: ExportReader,
    sink: S3ParquetSink,
    watermarks: WatermarkStore,
    tenant_id: str,
    dataset_ref: str,
    enabled: bool,
    batch_limit: int = 10_000,
) -> ExportResult:
    """Export one table for one tenant to partitioned Parquet. A no-op when export is disabled.

    ``dataset_ref`` is the S3 key prefix a table name appends to (``<prefix>``, e.g. ``tally/``).
    """
    previous = watermarks.get(tenant_id, spec.source_table)
    if not enabled:
        return ExportResult(spec.source_table, 0, 0, previous, previous)

    rows = reader.fetch_since(spec, tenant_id, previous, batch_limit)
    projected = [project_row(spec, r) for r in rows]

    # Group by day partition; write each partition idempotently (upsert on the natural key).
    by_partition: dict[date, list[dict[str, Any]]] = {}
    for raw, row in zip(rows, projected, strict=True):
        by_partition.setdefault(partition_key(spec, raw), []).append(row)

    written = 0
    for day, part_rows in sorted(by_partition.items()):
        written += sink.write_partition(
            table=spec.name,
            prefix=partition_prefix(dataset_ref, spec, day),
            schema=spec.schema,
            key_columns=spec.key_columns,
            rows=part_rows,
        )

    new_watermark = previous
    for raw in rows:
        ts = raw.get(spec.watermark_column)
        if ts is not None and (new_watermark is None or ts > new_watermark):
            new_watermark = ts
    if new_watermark is not None and new_watermark != previous:
        watermarks.advance(tenant_id, spec.source_table, new_watermark, written)

    return ExportResult(spec.source_table, written, len(by_partition), previous, new_watermark)


def run_export(
    *,
    reader: ExportReader,
    sink: S3ParquetSink,
    watermarks: WatermarkStore,
    tenant_id: str,
    dataset_ref: str,
    enabled: bool,
    batch_limit: int = 10_000,
    specs: Sequence[ExportTableSpec] = ALL_SPECS,
) -> list[ExportResult]:
    """Run every export table for one tenant, advancing each table's watermark independently."""
    return [
        export_table(
            spec=spec,
            reader=reader,
            sink=sink,
            watermarks=watermarks,
            tenant_id=tenant_id,
            dataset_ref=dataset_ref,
            enabled=enabled,
            batch_limit=batch_limit,
        )
        for spec in specs
    ]


# Convenience re-export so callers can look specs up by source table (shared with CTO-154).
__all__ = [
    "ALL_SPECS",
    "SPECS_BY_TABLE",
    "AthenaDependencyMissing",
    "Boto3S3ParquetSink",
    "ClickHouseExportReader",
    "ExportResult",
    "ExportTableSpec",
    "InMemoryS3ParquetSink",
    "S3ParquetSink",
    "all_ddl",
    "athena_create_table_ddl",
    "export_table",
    "load_s3_parquet_sink",
    "partition_key",
    "partition_prefix",
    "redshift_copy_sql",
    "run_export",
]
