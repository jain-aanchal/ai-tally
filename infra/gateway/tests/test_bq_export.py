"""BigQuery export sink (CTO-154).

Pins the export worker's guarantees with a faked reader + in-memory sink (no live ClickHouse or
BigQuery): the incremental watermark advances, a re-run doesn't duplicate (idempotency), no
body-keyed field is ever exported, export is disabled by default, and the optional dependency is
lazy-imported (the module imports without ``google-cloud-bigquery``).
"""

from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timezone

import pytest

from gateway import bq_export
from gateway.bq_export import (
    ALL_SPECS,
    DAILY_ROLLUP_SPEC,
    SPANS_SPEC,
    BigQueryDependencyMissing,
    ExportTableSpec,
    InMemoryBigQuerySink,
    InMemoryWatermarkStore,
    load_bigquery_sink,
    project_row,
    run_export,
    strip_body_keys,
)

UTC = timezone.utc
TENANT = "t-acme"
DATASET_REF = "proj.tally_export.tally_"


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
    sink = InMemoryBigQuerySink()
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
    assert len(sink) == 0
    assert marks.get(TENANT, "otel_spans") is None


# --- incremental watermark ----------------------------------------------------------------------

def test_watermark_advances_and_second_run_is_incremental() -> None:
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
    reader = _spans_reader([_span_row("tr1", "sp1", t0), _span_row("tr2", "sp2", t1)])
    sink = InMemoryBigQuerySink()
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
    t2 = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
    reader._rows["otel_spans"].append(_span_row("tr3", "sp3", t2))
    third = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
    )[0]
    assert third.rows_exported == 1
    assert marks.get(TENANT, "otel_spans") == t2
    assert len(sink.rows(f"{DATASET_REF}otel_spans")) == 3


# --- idempotency --------------------------------------------------------------------------------

def test_rerun_over_same_window_does_not_duplicate() -> None:
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    rows = [_span_row("tr1", "sp1", t0), _span_row("tr2", "sp2", t0)]
    reader = _spans_reader(rows)
    sink = InMemoryBigQuerySink()

    # Re-run the SAME window twice by resetting the watermark each time (simulates a replayed pass
    # after a crash). The sink upserts on (TenantId, TraceId, SpanId), so the table size is stable.
    for _ in range(3):
        run_export(
            reader=reader, sink=sink, watermarks=InMemoryWatermarkStore(), tenant_id=TENANT,
            dataset_ref=DATASET_REF, enabled=True, specs=(SPANS_SPEC,),
        )

    assert len(sink.rows(f"{DATASET_REF}otel_spans")) == 2


def test_sink_upsert_is_keyed_not_appended() -> None:
    sink = InMemoryBigQuerySink()
    sink.ensure_table("tbl", SPANS_SPEC.schema)
    row = {"TenantId": TENANT, "TraceId": "tr1", "SpanId": "sp1", "InputTokens": 1}
    sink.upsert_rows("tbl", SPANS_SPEC.key_columns, [row])
    sink.upsert_rows("tbl", SPANS_SPEC.key_columns, [{**row, "InputTokens": 99}])
    stored = sink.rows("tbl")
    assert len(stored) == 1
    assert stored[0]["InputTokens"] == 99


# --- no bodies ----------------------------------------------------------------------------------

def test_no_exported_column_is_body_keyed() -> None:
    for spec in ALL_SPECS:
        for col in spec.columns:
            assert not bq_export._is_body_key(col), f"{spec.name}.{col} is body-keyed"


def test_no_spec_sources_a_replay_body_table() -> None:
    banned = {"replay_runs", "replay_samples"}
    for spec in ALL_SPECS:
        assert spec.source_table not in banned
        # The CTO-125 candidate response field must never surface as a column either.
        assert not any(c.lower() in {"responsetext", "response_text"} for c in spec.columns)


def test_span_attribute_map_strips_body_keys() -> None:
    attrs = {
        "gen_ai.request.temperature": "0.7",
        "gen_ai.prompt": "SECRET USER PROMPT",
        "content": "SECRET BODY",
        "messages": "[...]",
        "region": "us-east1",
    }
    cleaned = strip_body_keys(attrs)
    assert cleaned == {"gen_ai.request.temperature": "0.7", "region": "us-east1"}


def test_project_row_body_strips_json_column() -> None:
    row = _span_row(
        "tr1", "sp1", datetime(2026, 7, 1, tzinfo=UTC),
        SpanAttributes={"model_kwargs": "x", "prompt": "LEAK", "body": "LEAK"},
    )
    projected = project_row(SPANS_SPEC, row)
    assert projected["SpanAttributes"] == {"model_kwargs": "x"}


# --- rollup shape -------------------------------------------------------------------------------

def test_rollup_merges_uniq_states_and_groups() -> None:
    # The uniq HLL states can't be exported raw; the SELECT must uniqMerge them, grouped by key.
    sql = DAILY_ROLLUP_SPEC.select_list()
    assert "uniqMerge(TraceCountState) AS TraceCount" in sql
    assert "uniqMerge(UserCountState) AS UserCount" in sql
    assert "sum(InputTokens) AS InputTokens" in sql
    assert DAILY_ROLLUP_SPEC.group_by == (
        "TenantId", "Day", "FeatureTag", "GenAiResponseModel"
    )

    day = date(2026, 7, 1)
    reader = FakeReader({
        "daily_feature_rollup": [{
            "TenantId": TENANT, "Day": day, "FeatureTag": "chat",
            "GenAiResponseModel": "gpt-4o", "InputTokens": 100, "OutputTokens": 50,
            "CachedInputTokens": 0, "EstimatedCost": 1, "ReconciledCost": 1,
            "SpanCount": 3, "TraceCount": 2, "UserCount": 1,
        }]
    })
    sink = InMemoryBigQuerySink()
    marks = InMemoryWatermarkStore()
    result = run_export(
        reader=reader, sink=sink, watermarks=marks, tenant_id=TENANT,
        dataset_ref=DATASET_REF, enabled=True, specs=(DAILY_ROLLUP_SPEC,),
    )[0]
    assert result.rows_exported == 1
    assert marks.get(TENANT, "daily_feature_rollup") == day


# --- lazy optional import -----------------------------------------------------------------------

def test_module_imports_without_bigquery_dependency() -> None:
    # The module imported at the top of this file (and is usable) without the optional extra,
    # which proves google-cloud-bigquery is never imported at module load. A fresh import via the
    # loader must likewise not pull it in eagerly.
    module = importlib.import_module("gateway.bq_export")
    assert hasattr(module, "run_export")
    assert "google.cloud.bigquery" not in sys.modules


def test_load_bigquery_sink_guards_missing_dependency() -> None:
    try:
        import google.cloud.bigquery  # noqa: F401
    except ImportError:
        with pytest.raises(BigQueryDependencyMissing):
            load_bigquery_sink("proj")
    else:  # pragma: no cover - extra installed in this env
        pytest.skip("google-cloud-bigquery is installed; lazy-import guard not exercisable")


def test_all_specs_have_consistent_schema_and_keys() -> None:
    for spec in ALL_SPECS:
        # Every key column must be an exported column.
        for key in spec.key_columns:
            assert key in spec.columns, f"{spec.name} key {key} not in columns"
        # Every JSON column must be declared as BQ JSON in the schema.
        json_fields = {f.name for f in spec.schema if f.type == "JSON"}
        assert set(spec.json_columns) <= json_fields
