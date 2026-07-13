# S3 / Athena export sink (CTO-160)

An **optional, additive** worker that mirrors a tenant's telemetry into the tenant's *own* S3 bucket
as partitioned Parquet, queryable through **Athena** (an external table / Glue catalog entry) and
loadable into **Redshift** with a `COPY` recipe. It is the AWS analog of the BigQuery export
([`docs/bigquery-export.md`](./bigquery-export.md), CTO-154). ClickHouse stays ai-tally's primary
telemetry store and the dashboard keeps reading it — S3/Athena is a **mirror, never a replacement**.
There is no reverse sync and no realtime-streaming SLA; batch / near-real-time passes are the v1
contract.

- Code: `infra/gateway/src/gateway/athena_export.py` (worker) and
  `infra/gateway/src/gateway/tenant_athena_export.py` (per-tenant config + watermark stores).
- **Shared with CTO-154 so both sinks emit identical facts:** the export specs
  (`gateway.bq_export.ALL_SPECS`), the `ClickHouseExportReader`, the `project_row` / `strip_body_keys`
  body-stripping, and the `WatermarkStore` protocol are all reused verbatim. Only the *sink* differs
  (Parquet-to-S3 instead of a BigQuery `MERGE`).
- Optional dependency: the `[athena]` extra (`pyarrow` + `boto3`), **lazy-imported** inside the real
  sink. The gateway imports and boots without it.
- Default: **disabled** — at the process level (`TALLY_ATHENA_EXPORT_ENABLED=false`) and per-tenant
  (`tenant_athena_export_config.enabled=false`, the state for any tenant with no row).

## What is exported

The same reconciled cost/attribution facts as the BigQuery export (identical columns, natural keys,
and incremental watermark columns — they are the *same* `ExportTableSpec` objects):

| S3 prefix / Athena table | ClickHouse source     | Incremental column   | Natural key (dedupe)                            |
| ------------------------ | --------------------- | -------------------- | ----------------------------------------------- |
| `otel_spans`             | `otel_spans`          | `Timestamp`          | `TenantId, TraceId, SpanId`                     |
| `business_events`        | `business_events`     | `OccurredAt`         | `TenantId, BusinessEventId`                     |
| `attribution_records`    | `attribution_records` | `AttributedTraceTs`  | `TenantId, BusinessEventId, FeatureTag`         |
| `daily_feature_rollup`   | `daily_feature_rollup`| `Day`                | `TenantId, Day, FeatureTag, GenAiResponseModel` |

### S3 layout + partitioning

Each table is written under `s3://<bucket>/<prefix>/<table>/`, Hive-partitioned by a day column `dt`
derived from the table's incremental watermark column:

```
s3://<bucket>/<prefix>/otel_spans/dt=2026-07-12/data.parquet
s3://<bucket>/<prefix>/daily_feature_rollup/dt=2026-07-12/data.parquet
```

`dt` is a **partition** column (declared with `PARTITIONED BY`), not a stored data column. Register
new partitions with `MSCK REPAIR TABLE <table>` (or a Glue crawler) after a pass.

### Type mapping

The Athena/Redshift schema is generated from the **same** `BQField` schema the BigQuery sink uses —
one schema, two dialects:

| BigQuery type | Athena / Parquet type |
| ------------- | --------------------- |
| `STRING`      | `string`              |
| `INT64`       | `bigint`              |
| `NUMERIC`     | `decimal(38, 9)`      |
| `FLOAT64`     | `double`              |
| `TIMESTAMP`   | `timestamp`           |
| `DATE`        | `date`                |
| `JSON`        | `string` (JSON map serialized as a JSON string) |

The long-tail attribute maps (`otel_spans.SpanAttributes`, `business_events.RawPayload`) are stored
as a JSON string column.

### Athena DDL / Redshift COPY

The DDL is generated programmatically from the shared specs:

```python
from gateway.athena_export import all_ddl, redshift_copy_sql, ALL_SPECS

ddl = all_ddl(database="tally_export", bucket="my-bucket", prefix="tally")
print(ddl["otel_spans"])
```

produces, e.g.:

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS `tally_export`.`otel_spans` (
  `TenantId` string,
  ...
  `EstimatedCost` decimal(38, 9),
  `SpanAttributes` string
)
PARTITIONED BY (`dt` date)
STORED AS PARQUET
LOCATION 's3://my-bucket/tally/otel_spans/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');
```

The optional Redshift `COPY` recipe loads the same Parquet:

```sql
COPY otel_spans
FROM 's3://my-bucket/tally/otel_spans/'
IAM_ROLE 'arn:aws:iam::<acct>:role/tally-redshift'
FORMAT AS PARQUET;
```

A ready-to-edit static copy of the four `CREATE EXTERNAL TABLE` statements lives in
[`db/athena/athena_external_tables.sql`](../db/athena/athena_external_tables.sql).

## No message bodies (by construction)

The export preserves ai-tally's "counts only, never bodies" invariant (CTO-118). Because the specs
are shared with CTO-154, the same guards apply:

1. The exported tables hold no message text to begin with — `otel_spans` already drops body-keyed
   attributes on ingest (`mapping.span_to_row`).
2. At import time the worker re-asserts no exported column is body-keyed (reusing the span-side
   `mapping._is_body_key` guard) and that no spec reads a replay table.
3. As a second, independent guard, `SpanAttributes` / `RawPayload` JSON maps are body-stripped again
   before they leave (`strip_body_keys`).

The replay **candidate-response text** (CTO-125, `replay_runs.ResponseText`) is a **separate, opt-in
tier** and is explicitly **excluded** here — no export spec sources a replay table. Covered by
`infra/gateway/tests/test_athena_export.py`: `test_no_exported_column_is_body_keyed`,
`test_no_spec_sources_a_replay_body_table`, `test_span_attribute_map_strips_body_keys_before_write`.

## Idempotency

Each pass reads rows strictly greater than the stored watermark, groups them by day partition, and
**upserts** each partition on the same natural key ClickHouse dedupes on (read-merge-write of the
partition's Parquet object). Re-running a pass — e.g. after a crash — never duplicates rows, and a
later pass that adds more rows to a day already partially exported keeps the earlier rows. The
watermark advances only to the max value actually seen. Covered by
`test_rerun_over_same_window_does_not_duplicate`,
`test_intraday_incremental_pass_keeps_earlier_rows_of_partition`, and
`test_watermark_advances_and_second_run_is_incremental`.

## Configuration

Process-level settings (env, `TALLY_`-prefixed — see `gateway/config.py`):

| Setting                                | Default        | Meaning                                        |
| -------------------------------------- | -------------- | ---------------------------------------------- |
| `TALLY_ATHENA_EXPORT_ENABLED`          | `false`        | Master kill-switch for the worker.             |
| `TALLY_ATHENA_EXPORT_S3_BUCKET`        | `""`           | Bucket owning the export prefixes.             |
| `TALLY_ATHENA_EXPORT_DEFAULT_PREFIX`   | `tally/`       | Default S3 key prefix for a newly-enabled tenant. |
| `TALLY_ATHENA_EXPORT_DEFAULT_DATABASE` | `tally_export` | Default Athena/Glue database.                  |
| `TALLY_ATHENA_EXPORT_AWS_REGION`       | `""`           | AWS region (else resolved from the environment). |
| `TALLY_ATHENA_EXPORT_BATCH_LIMIT`      | `10000`        | Max rows pulled per (tenant, table) pass.      |

Per-tenant config (`tenant_athena_export_config`, migration `db/postgres/0020_...`): `enabled`,
`bucket`, `prefix`, `database`, `region`, `credential_ref`. Incremental cursor
(`athena_export_watermarks`, migration `db/postgres/0021_...`): one row per (tenant, source table).

### Auth — reference only, never a raw key

`credential_ref` is a **reference**, not a secret: an IAM **role ARN** the worker assumes, or a
Secrets Manager / SSM parameter resource name. The gateway **rejects** anything that looks like an
inline AWS access key / secret (`tenant_athena_export._reject_raw_key`). At runtime the client uses
its default AWS credential chain (instance/role profile, IRSA, or an assumed role). Raw keys never
live in the database.

## Runbook

1. Install the optional extra on the worker host: `uv sync --extra athena`.
2. Create the bucket/prefix and grant the runtime identity (an assumed IAM role) `s3:GetObject` /
   `s3:PutObject` / `s3:ListBucket` on `s3://<bucket>/<prefix>/*`, plus Athena/Glue read as needed.
3. Enable for the tenant: set `tenant_athena_export_config.enabled=true` with the destination and a
   `credential_ref`. Set `TALLY_ATHENA_EXPORT_ENABLED=true` at the process level.
4. Invoke a pass per tenant (from a scheduler / cron):

   ```python
   from gateway.athena_export import ClickHouseExportReader, load_s3_parquet_sink, run_export
   from gateway.config import get_settings
   from gateway.store import ClickHouseStore
   from gateway.tenant_athena_export import AthenaExportWatermarkStore, TenantAthenaExportStore

   settings = get_settings()
   cfg = TenantAthenaExportStore(settings).get(tenant_id)
   run_export(
       reader=ClickHouseExportReader(ClickHouseStore(settings).client),
       sink=load_s3_parquet_sink(cfg.bucket, region=cfg.region),
       watermarks=AthenaExportWatermarkStore(settings),
       tenant_id=tenant_id,
       dataset_ref=cfg.dataset_ref(),
       enabled=cfg.enabled,
       batch_limit=settings.athena_export_batch_limit,
   )
   ```

5. Register partitions in Athena: `MSCK REPAIR TABLE <table>` (or run a Glue crawler).

The first pass backfills (watermark is `NULL`); subsequent passes are incremental. Re-running a pass
is safe.

## What's stubbed

`Boto3S3ParquetSink` (the real `pyarrow`-encode + `boto3` put, read-merge-write per partition) and
`ClickHouseExportReader` (shared with CTO-154) are implemented but exercised only against live S3 /
ClickHouse — the unit suite drives the in-memory fakes (`InMemoryS3ParquetSink`, `FakeReader`) so
the partitioning + incremental + idempotency + no-body logic is fully testable with no cloud
dependencies. Per-tenant *scheduling* (a running daemon / cron wiring) is intentionally out of scope;
`run_export` is the invocable entry point a scheduler calls.
