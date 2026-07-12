# BigQuery export sink (CTO-154)

An **optional, additive** worker that mirrors a tenant's telemetry into the tenant's *own*
BigQuery dataset for enterprise analytics. ClickHouse stays ai-tally's primary telemetry store and
the dashboard keeps reading it — BigQuery is a **mirror, never a replacement**. There is no reverse
sync and no realtime-streaming SLA; batch / near-real-time passes are the v1 contract.

- Code: `infra/gateway/src/gateway/bq_export.py` (worker) and
  `infra/gateway/src/gateway/tenant_bq_export.py` (per-tenant config + watermark stores).
- Optional dependency: the `[bigquery]` extra (`google-cloud-bigquery`), **lazy-imported** inside
  the worker. The gateway imports and boots without it.
- Default: **disabled** — at the process level (`TALLY_BQ_EXPORT_ENABLED=false`) and per-tenant
  (`tenant_bq_export_config.enabled=false`, the state for any tenant with no row).

## What is exported

| BigQuery table (prefix + name) | ClickHouse source     | Incremental column   | Natural key (dedupe)                          |
| ------------------------------ | --------------------- | -------------------- | --------------------------------------------- |
| `<prefix>otel_spans`           | `otel_spans`          | `Timestamp`          | `TenantId, TraceId, SpanId`                   |
| `<prefix>business_events`      | `business_events`     | `OccurredAt`         | `TenantId, BusinessEventId`                   |
| `<prefix>attribution_records`  | `attribution_records` | `AttributedTraceTs`  | `TenantId, BusinessEventId, FeatureTag`       |
| `<prefix>daily_feature_rollup` | `daily_feature_rollup`| `Day`                | `TenantId, Day, FeatureTag, GenAiResponseModel` |

Columns are schema-mapped 1:1 with typed BigQuery columns. ClickHouse → BigQuery type mapping:
`String`/`LowCardinality`/`FixedString`/`Enum8` → `STRING`; `DateTime64` → `TIMESTAMP`;
`Date` → `DATE`; integer types → `INT64`; `Decimal64(8)` → `NUMERIC`; `Float32` → `FLOAT64`.

The long-tail attribute maps are exported as a single BigQuery `JSON` column:
`otel_spans.SpanAttributes` → `JSON`, and `business_events.RawPayload` → `JSON`.

For `daily_feature_rollup`, the `AggregateFunction(uniq, …)` HLL states cannot be exported raw: the
reader `uniqMerge`s them into plain `INT64` counts (`TraceCount`, `UserCount`) and `sum()`s the
`SummingMergeTree` measures, grouped by the rollup's natural key.

The authoritative BigQuery schema for each table is the `schema` on each `ExportTableSpec` in
`bq_export.py` (`SPANS_SPEC`, `BUSINESS_EVENTS_SPEC`, `ATTRIBUTION_SPEC`, `DAILY_ROLLUP_SPEC`).

## No message bodies (by construction)

The export preserves ai-tally's "counts only, never bodies" invariant (CTO-118):

1. The exported tables hold no message text to begin with — `otel_spans` already drops body-keyed
   attributes on ingest (`mapping.span_to_row`).
2. At import time the worker asserts no exported column is body-keyed (reusing the span-side
   `mapping._is_body_key` guard) and that no spec reads a replay table.
3. As a second, independent guard, `SpanAttributes` / `RawPayload` JSON maps are body-stripped
   again before they leave (`strip_body_keys`).

The replay **candidate-response text** (CTO-125, `replay_runs.ResponseText`) is a **separate,
opt-in tier** and is explicitly **excluded** here — no export spec sources a replay table.

Covered by `infra/gateway/tests/test_bq_export.py`:
`test_no_exported_column_is_body_keyed`, `test_no_spec_sources_a_replay_body_table`,
`test_span_attribute_map_strips_body_keys`, `test_project_row_body_strips_json_column`.

## Idempotency

Each pass reads rows strictly greater than the stored watermark, then **upserts** into BigQuery on
the same natural key ClickHouse dedupes on (`MERGE ... ON <key>`). Re-running a pass — e.g. after a
crash — never duplicates rows. The watermark advances only to the max value actually seen. Covered
by `test_rerun_over_same_window_does_not_duplicate` and `test_watermark_advances_and_second_run_is_incremental`.

## Configuration

Process-level settings (env, `TALLY_`-prefixed — see `gateway/config.py`):

| Setting                              | Default         | Meaning                                        |
| ------------------------------------ | --------------- | ---------------------------------------------- |
| `TALLY_BQ_EXPORT_ENABLED`            | `false`         | Master kill-switch for the worker.             |
| `TALLY_BQ_EXPORT_GCP_PROJECT`        | `""`            | Project owning the datasets (or resolve ADC).  |
| `TALLY_BQ_EXPORT_DEFAULT_DATASET`    | `tally_export`  | Default dataset for a newly-enabled tenant.    |
| `TALLY_BQ_EXPORT_DEFAULT_TABLE_PREFIX` | `tally_`      | Default table-name prefix.                     |
| `TALLY_BQ_EXPORT_BATCH_LIMIT`        | `10000`         | Max rows pulled per (tenant, table) pass.      |

Per-tenant config (`tenant_bq_export_config`, migration `db/postgres/0009_...`): `enabled`,
`project_id`, `dataset`, `table_prefix`, `credential_ref`. Incremental cursor
(`bq_export_watermarks`, migration `db/postgres/0010_...`): one row per (tenant, source table).

### Auth — reference only, never a raw key

`credential_ref` is a **reference**, not a secret: an ADC hint, a Workload-Identity
service-account **email**, or a Secret Manager resource name. The gateway **rejects** anything that
looks like an inline service-account key (`tenant_bq_export._reject_raw_key`). At runtime the client
uses its default credential chain (ADC / Workload Identity / a `GOOGLE_APPLICATION_CREDENTIALS`
Secret Manager mount). Raw keys never live in the database.

## Runbook

1. Install the optional extra on the worker host: `uv sync --extra bigquery`.
2. Grant the runtime identity (Workload Identity SA) `roles/bigquery.dataEditor` on the tenant's
   dataset and `roles/bigquery.jobUser` on the project.
3. Create the dataset (`bq mk --dataset <project>:<dataset>`); tables are created on demand
   (`ensure_table`, `exists_ok=True`).
4. Enable for the tenant: set `tenant_bq_export_config.enabled=true` with the destination and a
   `credential_ref`. `TALLY_BQ_EXPORT_ENABLED=true` at the process level.
5. Invoke a pass per tenant (from a scheduler / cron):

   ```python
   from gateway.bq_export import ClickHouseExportReader, load_bigquery_sink, run_export
   from gateway.config import get_settings
   from gateway.store import ClickHouseStore
   from gateway.tenant_bq_export import BQExportWatermarkStore, TenantBQExportStore

   settings = get_settings()
   cfg = TenantBQExportStore(settings).get(tenant_id)
   run_export(
       reader=ClickHouseExportReader(ClickHouseStore(settings).client),
       sink=load_bigquery_sink(cfg.project_id),
       watermarks=BQExportWatermarkStore(settings),
       tenant_id=tenant_id,
       dataset_ref=cfg.dataset_ref(),
       enabled=cfg.enabled,
       batch_limit=settings.bq_export_batch_limit,
   )
   ```

The first pass backfills (watermark is `NULL`); subsequent passes are incremental. Re-running a
pass is safe.

## What's stubbed

`GoogleBigQuerySink` (the real load-into-staging + `MERGE` sink) and `ClickHouseExportReader` are
implemented but exercised only against live BigQuery / ClickHouse — the unit suite drives the
in-memory fakes (`InMemoryBigQuerySink`, `FakeReader`) so the incremental + idempotency + no-body
logic is fully testable with no cloud dependencies. Per-tenant *scheduling* (a running daemon /
cron wiring) is intentionally out of scope, exactly as in `reconciliation.py`; `run_export` is the
invocable entry point a scheduler calls.
