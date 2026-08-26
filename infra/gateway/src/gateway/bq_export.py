"""Optional BigQuery export sink for spans + attribution (CTO-154).

An **additive, opt-in mirror** of a tenant's telemetry into their *own* BigQuery dataset for
enterprise analytics. ClickHouse stays ai-tally's primary store and the dashboard keeps reading
it; this worker only copies rows out. It is never a replacement and never reads back into the
product.

What it mirrors, incrementally, per tenant:

* ``otel_spans``            — one BQ row per span (counts/tokens/cost, **never message text**)
* ``business_events``       — conversion / revenue events
* ``attribution_records``   — stitched value→feature attributions
* ``daily_feature_rollup``  — the daily cost/usage rollup (uniq states merged to plain counts)

Design mirrors the repo's existing patterns:

* a :class:`ReplayBlobStore`-style :class:`BigQuerySink` **Protocol** with an in-memory fake for
  tests and a lazily-constructed real client, so the module imports without the optional dep
  (exactly like the replay/GCS optional-backend split);
* a :mod:`gateway.reconciliation`-style pure orchestrator (``run_export``) over injected
  reader / sink / watermark stores, so the incremental + idempotency logic is unit-testable with
  no live ClickHouse or BigQuery;
* the ClickHouse read uses ``client.query(sql).result_rows`` — the same surface
  :meth:`gateway.store.ClickHouseStore.ping` uses.

**No bodies, by construction (CTO-118 / CTO-125).** The exported tables hold no message bodies:
we reuse the span-side ``_is_body_key`` guard to (a) assert at import time that no exported column
is body-keyed and (b) strip any body-keyed entry out of the ``SpanAttributes`` JSON map before it
leaves. The replay candidate-response text (CTO-125, ``replay_runs.ResponseText``) is a separate
opt-in tier and is *explicitly excluded* here — none of the export specs read a replay table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol

from gateway.mapping import COLUMNS as _SPAN_COLUMNS
from gateway.mapping import _is_body_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence


class BigQueryDependencyMissing(RuntimeError):
    """Raised when export is invoked but the optional ``[bigquery]`` extra isn't installed."""


# Replay tables carry candidate response bodies (CTO-125) under their own opt-in tier; the export
# sink must never source from them. Enforced at import time against every spec below.
_EXCLUDED_SOURCE_TABLES = frozenset({"replay_runs", "replay_samples"})


# --- Schema ------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BQField:
    """One BigQuery column. ``type`` is a standard-SQL type name (STRING/INT64/NUMERIC/...)."""

    name: str
    type: str
    mode: str = "NULLABLE"


@dataclass(frozen=True, slots=True)
class ExportTableSpec:
    """How one ClickHouse source table maps to one BigQuery table.

    * ``watermark_column`` — the monotonically-advancing column used for incremental reads.
    * ``key_columns``      — the same natural key ClickHouse dedupes on; the sink upserts on it so
                             re-runs never duplicate.
    * ``json_columns``     — columns exported as a BQ ``JSON`` column (the long-tail
                             ``SpanAttributes`` / ``RawPayload`` maps).
    * ``source_exprs``     — optional SQL expression overriding a column's source (used for the
                             rollup's ``uniqMerge``/``sum`` aggregates).
    * ``group_by``         — non-empty only for aggregate specs (the rollup); drives ``GROUP BY``.
    """

    name: str
    source_table: str
    watermark_column: str
    key_columns: tuple[str, ...]
    schema: tuple[BQField, ...]
    json_columns: tuple[str, ...] = ()
    source_exprs: dict[str, str] = field(default_factory=dict)
    group_by: tuple[str, ...] = ()

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.schema)

    def select_list(self) -> str:
        """The ``SELECT`` expression list, applying any ``source_exprs`` aliases."""
        parts = []
        for col in self.columns:
            expr = self.source_exprs.get(col)
            parts.append(f"{expr} AS {col}" if expr else col)
        return ", ".join(parts)


def _s(name: str) -> BQField:
    return BQField(name, "STRING")


# otel_spans: 1:1 typed columns straight from the gateway insert column list, with the long-tail
# SpanAttributes map exported as a single JSON column. Message text is never in this table.
_SPAN_TYPES: dict[str, str] = {
    "Timestamp": "TIMESTAMP",
    "StatusCode": "INT64",
    "DurationNs": "INT64",
    "InputTokens": "INT64",
    "OutputTokens": "INT64",
    "CachedInputTokens": "INT64",
    "EstimatedCost": "NUMERIC",
    "AgentStepIndex": "INT64",
    "ContextDroppedMessages": "INT64",
    "ContextDroppedTokens": "INT64",
    "ContextWindowUsedPct": "FLOAT64",
    "SamplingRate": "FLOAT64",
    "SpanAttributes": "JSON",
    "SampleRate": "FLOAT64",
}
SPANS_SPEC = ExportTableSpec(
    name="otel_spans",
    source_table="otel_spans",
    watermark_column="Timestamp",
    key_columns=("TenantId", "TraceId", "SpanId"),
    # Derived 1:1 from the gateway insert column list, so the account dimension CTO-180 added to
    # otel_spans (AccountIdHash + AccountIdHashKeyVersion) reaches this export automatically via
    # _SPAN_COLUMNS. Both are FixedString(64)/LowCardinality on the source and map to STRING here,
    # exactly like UserIdHash/UserIdHashKeyVersion. See CTO-202: a test asserts they are present.
    schema=tuple(BQField(c, _SPAN_TYPES.get(c, "STRING")) for c in _SPAN_COLUMNS),
    json_columns=("SpanAttributes",),
)

BUSINESS_EVENTS_SPEC = ExportTableSpec(
    name="business_events",
    source_table="business_events",
    watermark_column="OccurredAt",
    key_columns=("TenantId", "BusinessEventId"),
    schema=(
        _s("TenantId"),
        _s("BusinessEventId"),
        _s("EventName"),
        _s("UserIdHash"),
        # Account dimension (CTO-202). business_events.AccountIdHash is populated by CTO-195 but
        # was never added to this hand-written spec, so a tenant mirroring revenue events to their
        # own warehouse got rows with no account grouping and could not rebuild cost-per-customer.
        # Same treatment as UserIdHash: a FixedString(64) HMAC hash exported as STRING. There is no
        # AccountIdHashKeyVersion here because business_events carries no such column (unlike
        # otel_spans); see db/clickhouse/attribution.sql.
        _s("AccountIdHash"),
        BQField("OccurredAt", "TIMESTAMP"),
        BQField("IngestedAt", "TIMESTAMP"),
        BQField("ValueAmountMicro", "INT64"),
        _s("ValueCurrency"),
        _s("ValueType"),
        _s("Source"),
        BQField("RawPayload", "JSON"),
    ),
    json_columns=("RawPayload",),
)

ATTRIBUTION_SPEC = ExportTableSpec(
    name="attribution_records",
    source_table="attribution_records",
    watermark_column="AttributedTraceTs",
    key_columns=("TenantId", "BusinessEventId", "FeatureTag"),
    schema=(
        _s("TenantId"),
        _s("BusinessEventId"),
        _s("FeatureTag"),
        _s("AttributedTraceId"),
        BQField("AttributedTraceTs", "TIMESTAMP"),
        BQField("AttributedTraceCost", "NUMERIC"),
        BQField("ValueAmountMicro", "INT64"),
        _s("ValueCurrency"),
        _s("AttributionModel"),
        _s("AttributionConfidence"),
        _s("UserIdHashKeyVersion"),
        BQField("LookbackWindowDays", "INT64"),
        BQField("StitchedAt", "TIMESTAMP"),
        _s("StitcherVersion"),
    ),
)

# daily_feature_rollup: the uniq HLL states can't be exported raw — merge them to plain counts,
# and sum the SummingMergeTree measures across parts, grouped by the rollup's natural key.
DAILY_ROLLUP_SPEC = ExportTableSpec(
    name="daily_feature_rollup",
    source_table="daily_feature_rollup",
    watermark_column="Day",
    key_columns=("TenantId", "Day", "FeatureTag", "GenAiResponseModel"),
    schema=(
        _s("TenantId"),
        BQField("Day", "DATE"),
        _s("FeatureTag"),
        _s("GenAiResponseModel"),
        BQField("InputTokens", "INT64"),
        BQField("OutputTokens", "INT64"),
        BQField("CachedInputTokens", "INT64"),
        BQField("EstimatedCost", "NUMERIC"),
        BQField("ReconciledCost", "NUMERIC"),
        BQField("SpanCount", "INT64"),
        BQField("TraceCount", "INT64"),
        BQField("UserCount", "INT64"),
    ),
    source_exprs={
        "InputTokens": "sum(InputTokens)",
        "OutputTokens": "sum(OutputTokens)",
        "CachedInputTokens": "sum(CachedInputTokens)",
        "EstimatedCost": "sum(EstimatedCost)",
        "ReconciledCost": "sum(ReconciledCost)",
        "SpanCount": "sum(SpanCount)",
        "TraceCount": "uniqMerge(TraceCountState)",
        "UserCount": "uniqMerge(UserCountState)",
    },
    group_by=("TenantId", "Day", "FeatureTag", "GenAiResponseModel"),
)

ALL_SPECS: tuple[ExportTableSpec, ...] = (
    SPANS_SPEC,
    BUSINESS_EVENTS_SPEC,
    ATTRIBUTION_SPEC,
    DAILY_ROLLUP_SPEC,
)

SPECS_BY_TABLE: dict[str, ExportTableSpec] = {s.source_table: s for s in ALL_SPECS}


# --- No-bodies invariant, enforced at import ---------------------------------------------------

def _assert_no_body_columns() -> None:
    """Fail fast if any spec would ever export a body-keyed column or read a replay table."""
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
        # Belt-and-braces: the replay candidate-response field must never appear as a column.
        banned = {"responsetext", "response_text"}
        leaked = [c for c in spec.columns if c.lower() in banned]
        if leaked:
            raise AssertionError(f"export spec {spec.name!r} leaks replay body column {leaked!r}")


_assert_no_body_columns()


def strip_body_keys(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop any body-keyed entry from a long-tail attribute map before it is exported.

    ``SpanAttributes`` is already body-filtered on ingest (``mapping.span_to_row``); this is a
    second, independent guard so a body key can never reach BigQuery even if a raw row is fed in.
    """
    return {k: v for k, v in attrs.items() if not _is_body_key(str(k))}


def project_row(spec: ExportTableSpec, row: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw source row to exactly the exported columns, body-stripping JSON maps."""
    out: dict[str, Any] = {}
    for col in spec.columns:
        value = row.get(col)
        if col in spec.json_columns and isinstance(value, dict):
            value = strip_body_keys(value)
        out[col] = value
    return out


# --- Sink (Protocol + in-memory fake + lazy real client) ---------------------------------------

class BigQuerySink(Protocol):
    """Minimal put-only contract the exporter needs; keeps the worker BQ-client-agnostic."""

    def ensure_table(self, table_id: str, schema: Sequence[BQField]) -> None: ...

    def upsert_rows(
        self, table_id: str, key_columns: Sequence[str], rows: Sequence[dict[str, Any]]
    ) -> int: ...


class InMemoryBigQuerySink:
    """Dict-backed sink used by tests and local dev.

    Idempotent by the same natural key ClickHouse dedupes on: ``upsert_rows`` overwrites any row
    with a matching key tuple, so re-running an export never grows the table.
    """

    def __init__(self) -> None:
        self._tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
        self._schemas: dict[str, tuple[BQField, ...]] = {}

    def ensure_table(self, table_id: str, schema: Sequence[BQField]) -> None:
        self._tables.setdefault(table_id, {})
        self._schemas[table_id] = tuple(schema)

    def upsert_rows(
        self, table_id: str, key_columns: Sequence[str], rows: Sequence[dict[str, Any]]
    ) -> int:
        table = self._tables.setdefault(table_id, {})
        for row in rows:
            key = tuple(row.get(k) for k in key_columns)
            table[key] = dict(row)
        return len(rows)

    def rows(self, table_id: str) -> list[dict[str, Any]]:
        return list(self._tables.get(table_id, {}).values())

    def __len__(self) -> int:
        return sum(len(t) for t in self._tables.values())


class GoogleBigQuerySink:
    """Real sink over ``google-cloud-bigquery``. Constructed by :func:`load_bigquery_sink` only.

    Idempotency uses a load-into-staging + ``MERGE`` on the natural key, so a re-export upserts
    rather than appends. The BQ client is injected (already lazily imported) to keep this class
    importable for typing even when the extra is absent.

    NB: exercised only against a live BigQuery project — the unit suite drives
    :class:`InMemoryBigQuerySink`. See docs/bigquery-export.md for the runbook.
    """

    def __init__(self, client: Any) -> None:  # pragma: no cover - needs live BQ
        self._client = client

    def ensure_table(self, table_id: str, schema: Sequence[BQField]) -> None:  # pragma: no cover
        from google.cloud import bigquery  # lazy: optional dep

        fields = [bigquery.SchemaField(f.name, f.type, mode=f.mode) for f in schema]
        table = bigquery.Table(table_id, schema=fields)
        self._client.create_table(table, exists_ok=True)

    def upsert_rows(  # pragma: no cover - needs live BQ
        self, table_id: str, key_columns: Sequence[str], rows: Sequence[dict[str, Any]]
    ) -> int:
        from google.cloud import bigquery  # lazy: optional dep

        if not rows:
            return 0
        staging_id = f"{table_id}__staging"
        self._client.load_table_from_json(
            list(rows),
            staging_id,
            job_config=bigquery.LoadJobConfig(
                write_disposition="WRITE_TRUNCATE",
                schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
            ),
        ).result()
        on = " AND ".join(f"T.{k}=S.{k}" for k in key_columns)
        cols = list(rows[0].keys())
        set_clause = ", ".join(f"{c}=S.{c}" for c in cols)
        insert_cols = ", ".join(cols)
        insert_vals = ", ".join(f"S.{c}" for c in cols)
        self._client.query(
            f"MERGE `{table_id}` T USING `{staging_id}` S ON {on} "
            f"WHEN MATCHED THEN UPDATE SET {set_clause} "
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        ).result()
        return len(rows)


def load_bigquery_sink(project_id: str | None = None) -> BigQuerySink:
    """Lazily construct the real BigQuery sink; raises if the ``[bigquery]`` extra is absent.

    Auth is left to the client's default credential chain (ADC / Workload Identity / a
    ``GOOGLE_APPLICATION_CREDENTIALS`` Secret Manager mount) — this function never takes a raw key.
    """
    try:
        from google.cloud import bigquery  # noqa: PLC0415 - lazy optional import by design
    except ImportError as exc:  # pragma: no cover - depends on env
        raise BigQueryDependencyMissing(
            "BigQuery export requires the optional 'bigquery' extra: "
            "`uv sync --extra bigquery` (installs google-cloud-bigquery)."
        ) from exc
    return GoogleBigQuerySink(bigquery.Client(project=project_id or None))


# --- ClickHouse read source --------------------------------------------------------------------

class ExportReader(Protocol):
    """Reads incremental source rows since a watermark. Injected so the worker is CH-agnostic."""

    def fetch_since(
        self, spec: ExportTableSpec, tenant_id: str, watermark: datetime | date | None, limit: int
    ) -> list[dict[str, Any]]: ...


class ClickHouseExportReader:
    """Reads incremental rows via ``client.query(sql).result_rows`` (same surface as ``ping``)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch_since(  # pragma: no cover - needs live ClickHouse
        self, spec: ExportTableSpec, tenant_id: str, watermark: datetime | date | None, limit: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": int(limit)}
        sql = f"SELECT {spec.select_list()} FROM {spec.source_table} WHERE TenantId = %(tenant_id)s"
        if watermark is not None:
            params["watermark"] = watermark
            sql += f" AND {spec.watermark_column} > %(watermark)s"
        if spec.group_by:
            sql += " GROUP BY " + ", ".join(spec.group_by)
        sql += f" ORDER BY {spec.watermark_column} ASC LIMIT %(limit)s"
        result = self._client.query(sql, parameters=params)
        cols = list(result.column_names)
        return [dict(zip(cols, r, strict=False)) for r in result.result_rows]


# --- Watermark store (Postgres-backed; Protocol + in-memory fake) ------------------------------

class WatermarkStore(Protocol):
    """Persists the last-exported watermark per (tenant, source table)."""

    def get(self, tenant_id: str, source_table: str) -> datetime | date | None: ...

    def advance(
        self, tenant_id: str, source_table: str, watermark: datetime | date, rows: int
    ) -> None: ...


class InMemoryWatermarkStore:
    """Dict-backed watermark store for tests / local dev."""

    def __init__(self) -> None:
        self._marks: dict[tuple[str, str], datetime | date] = {}

    def get(self, tenant_id: str, source_table: str) -> datetime | date | None:
        return self._marks.get((tenant_id, source_table))

    def advance(
        self, tenant_id: str, source_table: str, watermark: datetime | date, rows: int
    ) -> None:
        self._marks[(tenant_id, source_table)] = watermark


# --- Orchestrator ------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExportResult:
    source_table: str
    rows_exported: int
    previous_watermark: datetime | date | None
    new_watermark: datetime | date | None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_table": self.source_table,
            "rows_exported": self.rows_exported,
            "previous_watermark": _iso(self.previous_watermark),
            "new_watermark": _iso(self.new_watermark),
        }


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def export_table(
    *,
    spec: ExportTableSpec,
    reader: ExportReader,
    sink: BigQuerySink,
    watermarks: WatermarkStore,
    tenant_id: str,
    dataset_ref: str,
    enabled: bool,
    batch_limit: int = 10_000,
) -> ExportResult:
    """Export one table for one tenant. A no-op returning zero rows when export is disabled.

    ``dataset_ref`` is the fully-qualified ``project.dataset.prefix`` a table name appends to.
    """
    previous = watermarks.get(tenant_id, spec.source_table)
    if not enabled:
        return ExportResult(spec.source_table, 0, previous, previous)

    table_id = f"{dataset_ref}{spec.name}"
    sink.ensure_table(table_id, spec.schema)

    rows = reader.fetch_since(spec, tenant_id, previous, batch_limit)
    projected = [project_row(spec, r) for r in rows]
    written = sink.upsert_rows(table_id, spec.key_columns, projected)

    new_watermark = previous
    for raw in rows:
        ts = raw.get(spec.watermark_column)
        if ts is not None and (new_watermark is None or ts > new_watermark):
            new_watermark = ts
    if new_watermark is not None and new_watermark != previous:
        watermarks.advance(tenant_id, spec.source_table, new_watermark, written)

    return ExportResult(spec.source_table, written, previous, new_watermark)


def run_export(
    *,
    reader: ExportReader,
    sink: BigQuerySink,
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
