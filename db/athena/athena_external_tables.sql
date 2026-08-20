-- Athena external tables for the S3 export sink (CTO-160) — GENERATED from the shared
-- gateway.bq_export specs via gateway.athena_export.all_ddl(). Edit `<bucket>`/`<prefix>` to taste,
-- or regenerate with:  python -c "from gateway.athena_export import all_ddl; ...".
--
-- After a load, register new day partitions:  MSCK REPAIR TABLE <table>;  (or run a Glue crawler).

CREATE EXTERNAL TABLE IF NOT EXISTS `tally_export`.`otel_spans` (
  `TenantId` string,
  `Timestamp` timestamp,
  `TraceId` string,
  `SpanId` string,
  `ParentSpanId` string,
  `ServiceName` string,
  `SpanName` string,
  `StatusCode` bigint,
  `DurationNs` bigint,
  `FeatureTag` string,
  `SessionId` string,
  `UserIdHash` string,
  `UserIdHashKeyVersion` string,
  `IdempotencyKey` string,
  `GenAiSystem` string,
  `GenAiRequestModel` string,
  `GenAiResponseModel` string,
  `GenAiOperation` string,
  `GenAiToolName` string,
  `InputTokens` bigint,
  `OutputTokens` bigint,
  `CachedInputTokens` bigint,
  `EstimatedCost` decimal(38, 9),
  `CostCurrency` string,
  `CostSource` string,
  `PriceCatalogVersion` string,
  `AgentRunId` string,
  `AgentStepIndex` bigint,
  `ContextDroppedMessages` bigint,
  `ContextDroppedTokens` bigint,
  `ContextWindowUsedPct` double,
  `SamplingStratum` string,
  `SamplingRate` double,
  `SpanAttributes` string,
  `SampleRate` double
)
PARTITIONED BY (`dt` date)
STORED AS PARQUET
LOCATION 's3://<bucket>/<prefix>/otel_spans/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');

CREATE EXTERNAL TABLE IF NOT EXISTS `tally_export`.`business_events` (
  `TenantId` string,
  `BusinessEventId` string,
  `EventName` string,
  `UserIdHash` string,
  `OccurredAt` timestamp,
  `IngestedAt` timestamp,
  `ValueAmountMicro` bigint,
  `ValueCurrency` string,
  `ValueType` string,
  `Source` string,
  `RawPayload` string
)
PARTITIONED BY (`dt` date)
STORED AS PARQUET
LOCATION 's3://<bucket>/<prefix>/business_events/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');

CREATE EXTERNAL TABLE IF NOT EXISTS `tally_export`.`attribution_records` (
  `TenantId` string,
  `BusinessEventId` string,
  `FeatureTag` string,
  `AttributedTraceId` string,
  `AttributedTraceTs` timestamp,
  `AttributedTraceCost` decimal(38, 9),
  `ValueAmountMicro` bigint,
  `ValueCurrency` string,
  `AttributionModel` string,
  `AttributionConfidence` string,
  `UserIdHashKeyVersion` string,
  `LookbackWindowDays` bigint,
  `StitchedAt` timestamp,
  `StitcherVersion` string
)
PARTITIONED BY (`dt` date)
STORED AS PARQUET
LOCATION 's3://<bucket>/<prefix>/attribution_records/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');

CREATE EXTERNAL TABLE IF NOT EXISTS `tally_export`.`daily_feature_rollup` (
  `TenantId` string,
  `Day` date,
  `FeatureTag` string,
  `GenAiResponseModel` string,
  `InputTokens` bigint,
  `OutputTokens` bigint,
  `CachedInputTokens` bigint,
  `EstimatedCost` decimal(38, 9),
  `ReconciledCost` decimal(38, 9),
  `SpanCount` bigint,
  `TraceCount` bigint,
  `UserCount` bigint
)
PARTITIONED BY (`dt` date)
STORED AS PARQUET
LOCATION 's3://<bucket>/<prefix>/daily_feature_rollup/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');


-- Optional: load the same Parquet into Redshift (IAM_ROLE is a role ARN reference).
-- COPY otel_spans
-- FROM 's3://<bucket>/<prefix>/otel_spans/'
-- IAM_ROLE 'arn:aws:iam::<acct>:role/tally-redshift'
-- FORMAT AS PARQUET;

-- COPY business_events
-- FROM 's3://<bucket>/<prefix>/business_events/'
-- IAM_ROLE 'arn:aws:iam::<acct>:role/tally-redshift'
-- FORMAT AS PARQUET;

-- COPY attribution_records
-- FROM 's3://<bucket>/<prefix>/attribution_records/'
-- IAM_ROLE 'arn:aws:iam::<acct>:role/tally-redshift'
-- FORMAT AS PARQUET;

-- COPY daily_feature_rollup
-- FROM 's3://<bucket>/<prefix>/daily_feature_rollup/'
-- IAM_ROLE 'arn:aws:iam::<acct>:role/tally-redshift'
-- FORMAT AS PARQUET;
