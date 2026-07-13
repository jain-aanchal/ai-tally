"""S3 / Athena export sink (CTO-160).

Pins the AWS export worker's guarantees with a faked reader + in-memory S3 sink (no live ClickHouse
or S3): the shared CTO-154 specs are exported to day-partitioned Parquet, the incremental watermark
advances, a re-run doesn't duplicate (idempotency, per day partition), no body-keyed field is ever
exported, export is disabled by default, the Athena/Redshift DDL maps the shared schema, and the
optional dependency is lazy-imported (the module imports without pyarrow / boto3).
"""

from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timezone

import pytest

from gateway import athena_export
from gateway.athena_export import (
    ALL_SPECS,
    AthenaDependencyMissing,
    ExportTableSpec,
    InMemoryS3ParquetSink,
    all_ddl,
    athena_create_table_ddl,
    load_s3_parquet_sink,
    partition_prefix,
    redshift_copy_sql,
    run_export,
)
from gateway.bq_export import (
    DAILY_ROLLUP_SPEC,
    SPANS_SPEC,
    strip_body_keys,
)
from gateway.tenant_athena_export import (
    DEFAULT_CONFIG,
    AthenaExportConfig,
    RawKeyRejected,
    _reject_raw_key,
)

UTC = timezone.utc
TENANT = "t-acme"
# S3 key prefix a table name appends to (mirrors dataset_ref in the BigQuery suite).
DATASET_REF = "tally/"


class FakeReader:
    """In-memory ExportReader: returns canned rows, honouring the watermark + limit like CH would."""

    def __init__(self, rows_by_table: dict[str, list[dict[str, object]]]) -> None:
        self._rows = rows_by_table

    def fetch_since(
        self,
        spec: ExportTableSpec,
        tenant_id: str,
        watermark: datetime | date | None,
        limit: int,
    ) -> list[dict[str, object]]:
        rows = [r for r in self._rows.get(spec.source_table, []) if r["TenantId"] == tenant_id]
        if watermark is not None:
            rows = [r for r in rows if r[spec.watermark_column] > watermark]
        rows.sort(key=lambda r: r[spec.watermark_column])
        return rows[:limit]


class InMemoryWatermarkStore:
    """Dict-backed watermark store for tests (same protocol the Postgres store implements)."""

    def __init__(self) -> None:
        self._marks: dict[tuple[str, str], datetime | date] = {}

    def get(self, tenant_id: str, source_table: str) -> datetime | date | None:
        return self._marks.get((tenant_id, source_table))

    def advance(
        self, tenant_id: str, source_table: str, watermark: datetime | date, rows: int
    ) -> None:
        self._marks[(tenant_id, source_table)] = watermark


def _span_row(trace: str, span: str, ts: datetime, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {c: "" for c in SPANS_SPEC.columns}
    row.update(
        TenantId=TENANT,
        TraceId=trace,
        SpanId=span,
        Timestamp=ts,
        InputTokens=10,
        OutputTokens=20,
        SpanAttributes={"gen_ai.request.temperature": "0.7"},
    )
    row.update(extra)
    return row


def _spans_reader(rows: list[dict[str, object]]) -> FakeReader:
    return FakeReader({"otel_spans": rows})


# --- disabled by default ------------------------------------------------------------------------

def test_export_disabled_by_default_is_a_noop() -> None:
    reader = _spans_reader([_span_row("tr1", "sp1", datetime(2026, 7, 1, tzinfo=UTC))])
    sink = InMemoryS3ParquetSink()
    marks = InMemoryWatermarkStore()

    results = run_export(
        reader=reader,
        sink=sink,
        watermarks=marks,
        tenant_id=TENANT,
        dataset_ref=DATASET_REF,
        enabled=False,
        specs=(SPANS_SPEC,),
    )

    assert results[0].rows_exported == 0
    assert results[0].partitions_written == 0
    assert len(sink) == 0
    assert marks.get(TENANT, "otel_spans") is None


def test_tenant_config_default_is_disabled() -> None:
    assert DEFAULT_CONFIG.enabled is False
    assert DEFAULT_CONFIG.bucket == ""


# --- partitioning + incremental watermark -------------------------------------------------------

def test_rows_land_in_day_partitions_by_watermark() -> None:
    d1 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    d2 = datetime(2026, 7, 2, 9, 0, tzinfo=UTC)
    reader = _spans_reader([_span_row("tr1", "sp1", d1), _span_row("tr2", "sp2", d2)])
    sink = InMemoryS3ParquetSink()
    marks = InMemoryWatermarkStore()

    result = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )[0]

    assert result.rows_exported == 2
    assert result.partitions_written == 2
    assert partition_prefix(DATASET_REF, SPANS_SPEC, date(2026, 7, 1)) in sink.partitions
    assert partition_prefix(DATASET_REF, SPANS_SPEC, date(2026, 7, 2)) in sink.partitions


def test_watermark_advances_and_second_run_is_incremental() -> None:
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
    reader = _spans_reader([_span_row("tr1", "sp1", t0), _span_row("tr2", "sp2", t1)])
    sink = InMemoryS3ParquetSink()
    marks = InMemoryWatermarkStore()

    first = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )[0]
    assert first.rows_exported == 2
    assert first.previous_watermark is None
    assert marks.get(TENANT, "otel_spans") == t1

    # Nothing new -> second pass exports zero and leaves the watermark put.
    second = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )[0]
    assert second.rows_exported == 0
    assert marks.get(TENANT, "otel_spans") == t1

    # A newer row -> only that one is picked up, watermark advances again.
    t2 = datetime(2026, 7, 3, 14, 0, tzinfo=UTC)
    reader._rows["otel_spans"].append(_span_row("tr3", "sp3", t2))
    third = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )[0]
    assert third.rows_exported == 1
    assert marks.get(TENANT, "otel_spans") == t2
    # Three spans total across the table's partitions.
    assert len(sink.table_rows(f"{DATASET_REF}{SPANS_SPEC.name}/")) == 3


# --- idempotency (per day partition) ------------------------------------------------------------

def test_rerun_over_same_window_does_not_duplicate() -> None:
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    rows = [_span_row("tr1", "sp1", t0), _span_row("tr2", "sp2", t0)]
    reader = _spans_reader(rows)
    sink = InMemoryS3ParquetSink()

    # Re-run the SAME window twice by resetting the watermark each time (simulates a replayed pass
    # after a crash). The partition upserts on (TenantId, TraceId, SpanId), so it stays stable.
    for _ in range(3):
        run_export(
            reader=reader, sink=sink, watermarks=InMemoryWatermarkStore(), tenant_id=TENANT,
            dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
        )

    prefix = partition_prefix(DATASET_REF, SPANS_SPEC, date(2026, 7, 1))
    assert len(sink.rows(prefix)) == 2
    assert sink.write_count == 3  # three passes, but the partition never grew


def test_intraday_incremental_pass_keeps_earlier_rows_of_partition() -> None:
    # Two rows for the same day arriving across two passes must both survive (partition upsert,
    # not overwrite) — the failure mode a naive "overwrite the day file" sink would have.
    t_early = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    t_late = datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    reader = _spans_reader([_span_row("tr1", "sp1", t_early)])
    sink = InMemoryS3ParquetSink()
    marks = InMemoryWatermarkStore()

    run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )
    reader._rows["otel_spans"].append(_span_row("tr2", "sp2", t_late))
    run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )

    prefix = partition_prefix(DATASET_REF, SPANS_SPEC, date(2026, 7, 1))
    assert len(sink.rows(prefix)) == 2


def test_partition_write_is_keyed_not_appended() -> None:
    sink = InMemoryS3ParquetSink()
    prefix = partition_prefix(DATASET_REF, SPANS_SPEC, date(2026, 7, 1))
    row = {"TenantId": TENANT, "TraceId": "tr1", "SpanId": "sp1", "InputTokens": 1}
    sink.write_partition(
        table=SPANS_SPEC.name, prefix=prefix, schema=SPANS_SPEC.schema,
        key_columns=SPANS_SPEC.key_columns, rows=[row],
    )
    sink.write_partition(
        table=SPANS_SPEC.name, prefix=prefix, schema=SPANS_SPEC.schema,
        key_columns=SPANS_SPEC.key_columns, rows=[{**row, "InputTokens": 99}],
    )
    stored = sink.rows(prefix)
    assert len(stored) == 1
    assert stored[0]["InputTokens"] == 99


# --- no bodies ----------------------------------------------------------------------------------

def test_no_exported_column_is_body_keyed() -> None:
    for spec in ALL_SPECS:
        for col in spec.columns:
            assert not athena_export._is_body_key(col), f"{spec.name}.{col} is body-keyed"


def test_no_spec_sources_a_replay_body_table() -> None:
    banned = {"replay_runs", "replay_samples"}
    for spec in ALL_SPECS:
        assert spec.source_table not in banned
        assert not any(c.lower() in {"responsetext", "response_text"} for c in spec.columns)


def test_span_attribute_map_strips_body_keys_before_write() -> None:
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    row = _span_row(
        "tr1", "sp1", t0,
        SpanAttributes={"model_kwargs": "x", "prompt": "LEAK", "body": "LEAK"},
    )
    sink = InMemoryS3ParquetSink()
    run_export(
        reader=_spans_reader([row]), sink=sink, watermarks=InMemoryWatermarkStore(),
        tenant_id=TENANT, dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )
    prefix = partition_prefix(DATASET_REF, SPANS_SPEC, date(2026, 7, 1))
    stored = sink.rows(prefix)[0]
    assert stored["SpanAttributes"] == {"model_kwargs": "x"}


def test_strip_body_keys_reused_from_shared_module() -> None:
    cleaned = strip_body_keys({"region": "us-east1", "prompt": "SECRET", "content": "SECRET"})
    assert cleaned == {"region": "us-east1"}


# --- Athena / Redshift DDL ----------------------------------------------------------------------

def test_athena_ddl_maps_shared_schema_and_partitions_by_day() -> None:
    ddl = athena_create_table_ddl(
        SPANS_SPEC, database="tally_export", s3_location="s3://bkt/tally/otel_spans/"
    )
    assert "CREATE EXTERNAL TABLE IF NOT EXISTS `tally_export`.`otel_spans`" in ddl
    assert "PARTITIONED BY (`dt` date)" in ddl
    assert "STORED AS PARQUET" in ddl
    assert "LOCATION 's3://bkt/tally/otel_spans/'" in ddl
    # Type mapping from the shared BQField schema.
    assert "`TenantId` string" in ddl
    assert "`InputTokens` bigint" in ddl
    assert "`EstimatedCost` decimal(38, 9)" in ddl
    assert "`Timestamp` timestamp" in ddl
    assert "`SpanAttributes` string" in ddl  # JSON map stored as a JSON string
    # The partition column must NOT also be a data column.
    assert "`dt` " not in ddl.split("PARTITIONED BY")[0]


def test_daily_rollup_ddl_has_date_partition_and_date_column() -> None:
    ddl = athena_create_table_ddl(
        DAILY_ROLLUP_SPEC, database="db", s3_location="s3://bkt/tally/daily_feature_rollup/"
    )
    assert "`Day` date" in ddl
    assert "PARTITIONED BY (`dt` date)" in ddl


def test_all_ddl_covers_every_spec() -> None:
    ddl = all_ddl(database="tally_export", bucket="bkt", prefix="tally")
    assert set(ddl) == {s.name for s in ALL_SPECS}
    for spec in ALL_SPECS:
        assert f"s3://bkt/tally/{spec.name}/" in ddl[spec.name]


def test_redshift_copy_recipe_references_role_not_key() -> None:
    sql = redshift_copy_sql(
        SPANS_SPEC, s3_location="s3://bkt/tally/otel_spans/",
        iam_role="arn:aws:iam::123456789012:role/tally-redshift",
    )
    assert "COPY otel_spans" in sql
    assert "FORMAT AS PARQUET" in sql
    assert "IAM_ROLE 'arn:aws:iam::123456789012:role/tally-redshift'" in sql


# --- rollup shape (shared spec) -----------------------------------------------------------------

def test_rollup_exports_and_partitions_by_day() -> None:
    day = date(2026, 7, 1)
    reader = FakeReader({
        "daily_feature_rollup": [{
            "TenantId": TENANT, "Day": day, "FeatureTag": "chat",
            "GenAiResponseModel": "gpt-4o", "InputTokens": 100, "OutputTokens": 50,
            "CachedInputTokens": 0, "EstimatedCost": 1, "ReconciledCost": 1,
            "SpanCount": 3, "TraceCount": 2, "UserCount": 1,
        }]
    })
    sink = InMemoryS3ParquetSink()
    marks = InMemoryWatermarkStore()
    result = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(DAILY_ROLLUP_SPEC,),
    )[0]
    assert result.rows_exported == 1
    assert result.partitions_written == 1
    assert marks.get(TENANT, "daily_feature_rollup") == day
    assert partition_prefix(DATASET_REF, DAILY_ROLLUP_SPEC, day) in sink.partitions


# --- credential-ref guard (reference only, never a raw key) -------------------------------------

def test_reject_inline_aws_key() -> None:
    with pytest.raises(RawKeyRejected):
        _reject_raw_key("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG")
    with pytest.raises(RawKeyRejected):
        _reject_raw_key("AKIAIOSFODNN7EXAMPLE")


def test_accept_role_arn_reference() -> None:
    # An IAM role ARN is a reference, not a raw key — must be accepted.
    _reject_raw_key("arn:aws:iam::123456789012:role/tally-export")


def test_config_s3_uri_and_dataset_ref() -> None:
    cfg = AthenaExportConfig(
        enabled=True, bucket="bkt", prefix="tally", database="db", region="us-east-1",
        credential_ref="arn:aws:iam::1:role/r",
    )
    assert cfg.dataset_ref() == "tally/"
    assert cfg.s3_uri("otel_spans") == "s3://bkt/tally/otel_spans/"


# --- lazy optional import -----------------------------------------------------------------------

def test_module_imports_without_athena_dependency() -> None:
    # The module imported at the top of this file (and is usable) without the optional extra,
    # which proves pyarrow / boto3 are never imported at module load. A fresh import via the loader
    # must likewise not pull them in eagerly.
    module = importlib.import_module("gateway.athena_export")
    assert hasattr(module, "run_export")
    assert "pyarrow" not in sys.modules
    assert "boto3" not in sys.modules


def test_load_s3_parquet_sink_guards_missing_dependency() -> None:
    try:
        import boto3  # noqa: F401
        import pyarrow  # noqa: F401
    except ImportError:
        with pytest.raises(AthenaDependencyMissing):
            load_s3_parquet_sink("bkt")
    else:  # pragma: no cover - extra installed in this env
        pytest.skip("pyarrow/boto3 installed; lazy-import guard not exercisable")


def test_shared_specs_are_the_same_objects_as_bigquery() -> None:
    # Both sinks emit identical facts: athena_export re-exports the very same spec objects.
    from gateway import bq_export

    assert athena_export.ALL_SPECS is bq_export.ALL_SPECS
    for spec in ALL_SPECS:
        for key in spec.key_columns:
            assert key in spec.columns, f"{spec.name} key {key} not in columns"
