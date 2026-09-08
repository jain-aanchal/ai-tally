-- otel_spans — primary span table (ai-tally telemetry store)
-- Implements CTO-22. Spec §5.1.
--
-- Shared multi-tenant cluster (CTO-18): TenantId is FIRST in ORDER BY and is load-bearing —
-- every read must be tenant-scoped or it scans the whole cluster.
--
-- Cost is Decimal64(8) (money, never Float64). Dual-track: EstimatedCost + ReconciledCost +
-- CostSource. UserIdHashKeyVersion supports HMAC versioned-key rotation (CTO-74).
-- High-value attributes are promoted to typed columns; the long tail stays in SpanAttributes.

CREATE TABLE IF NOT EXISTS otel_spans
(
    TenantId               LowCardinality(String),
    Timestamp              DateTime64(9)            CODEC(Delta, ZSTD(1)),
    TraceId                String                   CODEC(ZSTD(1)),
    SpanId                 String                   CODEC(ZSTD(1)),
    ParentSpanId           String                   CODEC(ZSTD(1)),

    ServiceName            LowCardinality(String),
    SpanName               LowCardinality(String),
    StatusCode             UInt8,
    DurationNs             UInt64                   CODEC(T64, ZSTD(1)),

    -- Business / attribution
    FeatureTag             LowCardinality(String),
    SessionId              String                   CODEC(ZSTD(1)),
    UserIdHash             FixedString(64)          CODEC(ZSTD(1)),  -- HMAC-SHA256 hex
    UserIdHashKeyVersion   LowCardinality(String),                  -- HMAC rotation (CTO-74)

    -- Account dimension (CTO-180). The tenant's own paying customer: the company, workspace or
    -- team that the end user belongs to. It sits between TenantId (the ai-tally customer) and
    -- UserIdHash (an individual end user), which is a gap nothing filled before. Cost per USER
    -- already works because UserIdHash is on every span; cost per CUSTOMER needs this grouping.
    --
    -- Same type and same treatment as UserIdHash: HMAC-SHA256 hex under the per-tenant key, so
    -- an account hash cannot be reversed and cannot be joined across tenants. Raw account ids
    -- never reach this table. AccountIdHashKeyVersion mirrors UserIdHashKeyVersion so an account
    -- survives a key rotation the same way a user does (CTO-74).
    --
    -- DEFAULT '' is load-bearing. Every span written before this column existed, and every span
    -- from a tenant that has not instrumented account_id, reads back as ''. That is the
    -- UNATTRIBUTED bucket and callers must render it as such: it is not a customer named
    -- "unknown", and it must never be ranked alongside real accounts.
    --
    -- There is deliberately NO account label / display name column here. A label is mutable
    -- metadata: stamping it on every span wastes storage and creates a "which label wins"
    -- question the moment an account is renamed. It would also put customer names in the
    -- telemetry store, which is precisely what hashing the id exists to prevent. Labels live in
    -- the Postgres control plane, keyed on this hash and joined at render time.
    AccountIdHash          FixedString(64) DEFAULT ''  CODEC(ZSTD(1)),  -- HMAC-SHA256 hex
    AccountIdHashKeyVersion LowCardinality(String),                     -- HMAC rotation (CTO-74)
    IdempotencyKey         String                   CODEC(ZSTD(1)),

    -- GenAI core (gen_ai.* semconv)
    GenAiSystem            LowCardinality(String),
    GenAiRequestModel      LowCardinality(String),
    GenAiResponseModel     LowCardinality(String),
    GenAiOperation         LowCardinality(String),
    GenAiToolName          LowCardinality(String),
    -- CTO-244: usage is NULLABLE because "we do not know" is a real, common state and it is not 0.
    -- The motivating case is a streamed response through the edge proxy: usage cannot be scanned
    -- off the stream, so the provider never tells us how many tokens the call consumed. Storing 0
    -- there made the dashboard read a real, billed call as one that consumed nothing, silently
    -- understating spend. A provider-reported 0 still stores as 0 and stays distinguishable from
    -- NULL. T64 is retained: it composes with Nullable on integer types.
    InputTokens            Nullable(UInt32)         CODEC(T64, ZSTD(1)),
    OutputTokens           Nullable(UInt32)         CODEC(T64, ZSTD(1)),
    CachedInputTokens      Nullable(UInt32)         CODEC(T64, ZSTD(1)),

    -- Cost (dual-track; Decimal64(8), NOT Float64)
    --
    -- CTO-244: EstimatedCost is NULLABLE for the same reason. A price-catalog miss, or usage we
    -- never learned, cannot be priced, and $0.00 is a lie about a call that really did cost money.
    -- CostSource carries the WHY: 'unpriced' means we could not put a number on this span, so a
    -- reader can say so instead of summing a fabricated zero. PriceCatalogVersion is '' on those
    -- rows, which is the same empty-version signal tally.pricing already uses for a catalog miss.
    EstimatedCost          Nullable(Decimal64(8))   CODEC(ZSTD(1)),
    ReconciledCost         Nullable(Decimal64(8))   CODEC(ZSTD(1)),
    CostCurrency           LowCardinality(String),
    CostSource             Enum8('estimated' = 1, 'reconciled' = 2, 'unpriced' = 3),
    PriceCatalogVersion    LowCardinality(String),

    -- Agent context
    AgentRunId             String                   CODEC(ZSTD(1)),
    AgentStepIndex         UInt16,

    -- Context-window drops (CTO-118). Counts only — never the dropped message text.
    -- All three default to 0 so existing rows survive an additive ALTER without backfill.
    ContextDroppedMessages UInt32 DEFAULT 0         CODEC(T64, ZSTD(1)),
    ContextDroppedTokens   UInt32 DEFAULT 0         CODEC(T64, ZSTD(1)),
    ContextWindowUsedPct   Float32 DEFAULT 0        CODEC(ZSTD(1)),

    -- Stratified-sampling provenance (CTO-119). SamplingStratum is the head-time classification
    -- ('body'|'mid'|'tail'); SamplingRate is THAT stratum's configured keep rate. Distinct from
    -- the per-span `SampleRate` weight below (which is the billing-extrapolation factor — they're
    -- usually equal today, but conceptually independent: rate can change after a span is kept).
    -- Default 'unsampled' / 1.0 so pre-CTO-119 rows group as a separate, honestly-labelled bucket.
    SamplingStratum        LowCardinality(String) DEFAULT 'unsampled',
    SamplingRate           Float32 DEFAULT 1.0     CODEC(ZSTD(1)),

    -- Replay (Workflow 1)
    ResolvedPromptHash     FixedString(64),
    ResolvedContextRef     String,

    -- Long tail
    SpanAttributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    SpanEvents             Array(Tuple(name String, ts DateTime64(9), attrs Map(String, String))),

    -- Sampling
    SampleRate             Float32 DEFAULT 1.0,

    INDEX idx_trace_id     TraceId                  TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_session_id   SessionId                TYPE bloom_filter(0.01)  GRANULARITY 4,
    INDEX idx_user_id      UserIdHash               TYPE bloom_filter(0.01)  GRANULARITY 4,
    INDEX idx_agent_run    AgentRunId               TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_attr_keys    mapKeys(SpanAttributes)  TYPE bloom_filter(0.01)  GRANULARITY 4
)
-- CTO-245: ReplacingMergeTree, and TraceId/SpanId APPENDED to the sorting key. Both halves of that
-- are load-bearing and neither works without the other.
--
-- WHY AT ALL. This was a plain MergeTree, so a span written twice stayed twice, forever. Batch
-- idempotency was in-process only, so a client retrying a batch across a gateway restart wrote its
-- spans again and inflated every cost sum by exactly the replayed spend. One local run left 333,689
-- rows holding 271,571 distinct SpanIds. The primary fix is the durable idempotency store (see
-- db/postgres/0032_ingest_batch_idempotency.sql); this engine is the BACKSTOP for whatever slips
-- past it, and it is the same pattern business_events has used since CTO-176.
--
-- WHY THE SORTING KEY HAD TO CHANGE. ReplacingMergeTree collapses rows that agree on the SORTING
-- KEY, not on whatever a reader considers identity. The old key was
-- (TenantId, FeatureTag, ServiceName, SpanName, Timestamp), which contains no span identity at all:
-- switching engines without touching it would have deduped on the WRONG THING and silently deleted
-- genuinely distinct spans that happened to share a feature, service, name and timestamp. That is a
-- far worse bug than the one being fixed. TraceId and SpanId are therefore appended, making the key
-- (TenantId, ..., Timestamp, TraceId, SpanId) so that the collapsing identity really is one span.
-- Appending rather than reordering is deliberate: the old key stays a PREFIX of the new one, so
-- every existing query keeps exactly the index it had and no read gets slower.
--
-- NO VERSION COLUMN, on purpose. ReplacingMergeTree(<version>) exists to pick a winner among rows
-- that differ. The rows this collapses do not differ: they are the same span re-posted from the same
-- batch. There is nothing to arbitrate, and inventing a version column would only make it look as
-- though this table supports updating a span in place. It does not; the gateway only ever inserts
-- (gateway/store.py has the single insert site and there is no UPDATE path).
--
-- WHAT THIS DOES NOT FIX, stated plainly rather than left to be discovered:
--
--   1. Collapsing happens AT MERGE TIME and only WITHIN a partition. Partitioning is
--      toDate(Timestamp) and a duplicate carries the same Timestamp, so the partition condition
--      holds; the timing one does not. Between the duplicate insert and the background merge, a
--      plain `SELECT sum(...)` still sees both rows. Reads are never worse than before this change
--      (previously the duplicate was permanent), but they are not exact until the merge lands. A
--      read that needs exactness must say FINAL, and no read path was converted in this change.
--   2. The materialized views. daily_feature_rollup_mv, hourly_feature_rollup_mv (rollups.sql) and
--      daily_account_rollup_mv (account_rollups.sql) fire on INSERT into this table, so a duplicate
--      insert is summed into their SummingMergeTree targets before this engine ever sees it, and no
--      later merge here removes it. A duplicate that reaches ClickHouse is therefore PERMANENT in
--      the rollups regardless of this change. That is the strongest argument for the durable
--      idempotency store being the real fix and this being only a backstop. Repairing rollups that
--      already absorbed duplicates is CTO-311: db/clickhouse/checks/rollup_drift.sql measures it and
--      db/clickhouse/migrations/rollup_rebuild_from_spans.sql re-derives the affected grains from
--      this table. It can only repair grains this table still holds rows for; anything past
--      retention is unrecoverable and is left alone rather than guessed at.
--   3. A replay whose row is not byte-identical. Timestamp comes from the client's span timestamp
--      when present, so an ordinary replay reproduces the same row and collapses. A span with no
--      client timestamp, or one whose skew assessment clamps against server receive time, can land
--      on a different Timestamp on the replay and will NOT collapse. Nothing here can repair that;
--      only preventing the duplicate write can.
ENGINE = ReplacingMergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (TenantId, FeatureTag, ServiceName, SpanName, Timestamp, TraceId, SpanId)
-- Tiering (CTO-29): hot SSD -> warm volume at 7d -> cold volume at 30d -> drop raw at 90d.
-- This TTL is GENERATED from tally.storage_tiering.DEFAULT_POLICY (render_ttl_clause), the single
-- source of truth that also classifies a span's tier at query time, so DDL and logic can't drift.
-- NOTE: ClickHouse `TTL ... GROUP BY` requires its keys to be a prefix of the primary key, so we
-- deliberately do NOT aggregate-on-expire here (toDate(Timestamp)/GenAiResponseModel are not a PK
-- prefix). The surviving long-horizon aggregate lives in the rollup materialized views (CTO-24,
-- daily_feature_rollup), which persist independently of this raw table's retention — so trends and
-- late billing true-ups keep working after the raw span is dropped. Storage volumes ('warm',
-- 'cold') are configured in the ClickHouse storage policy (infra, CTO-94). Per-tenant retention
-- overrides (enterprise = longer) compile to a multiIf DELETE expression — see storage_tiering.sql.
TTL
    toDateTime(Timestamp) + INTERVAL 7 DAY  TO VOLUME 'warm',
    toDateTime(Timestamp) + INTERVAL 30 DAY TO VOLUME 'cold',
    toDateTime(Timestamp) + INTERVAL 90 DAY DELETE;

-- CTO-118 additive migration. Idempotent: `ADD COLUMN IF NOT EXISTS` plus a `DEFAULT 0`
-- so the operation is metadata-only and non-blocking; existing rows show 0 until they're
-- naturally aged out. Counts only — there is no body field here, and never will be.
ALTER TABLE otel_spans
    ADD COLUMN IF NOT EXISTS ContextDroppedMessages UInt32  DEFAULT 0 CODEC(T64, ZSTD(1)),
    ADD COLUMN IF NOT EXISTS ContextDroppedTokens   UInt32  DEFAULT 0 CODEC(T64, ZSTD(1)),
    ADD COLUMN IF NOT EXISTS ContextWindowUsedPct   Float32 DEFAULT 0 CODEC(ZSTD(1));

-- CTO-119 additive migration. Same idempotent pattern; default 'unsampled' / 1.0 means
-- pre-migration rows group as their own bucket on the DQ surface rather than polluting
-- body/mid/tail breakdowns.
ALTER TABLE otel_spans
    ADD COLUMN IF NOT EXISTS SamplingStratum LowCardinality(String) DEFAULT 'unsampled',
    ADD COLUMN IF NOT EXISTS SamplingRate    Float32                DEFAULT 1.0 CODEC(ZSTD(1));

-- CTO-180 additive migration. Same idempotent pattern as CTO-118/CTO-119: `ADD COLUMN IF NOT
-- EXISTS` plus a DEFAULT makes this metadata-only, so it applies to an already-populated table
-- without rewriting a single part and without blocking ingest. There is no backfill step because
-- there is nothing to backfill from: no span ever carried an account id, so historical rows stay
-- '' and are reported as unattributed rather than guessed at.
--
-- APPLYING THIS TO AN EXISTING DEPLOYMENT. The compose initdb directory that mounts this file
-- runs ONLY on a first boot against an empty volume, so a stack that is already up will never
-- see the statement below on its own. Replay the canonical DDL with `make ch-migrate` from
-- infra/ (every statement in db/clickhouse is IF NOT EXISTS, so replaying is safe and repeatable).
-- This is not a hypothetical: the Postgres side of this repo has already shipped migrations
-- (0011, 0012, 0015, 0016) that silently never reached an existing volume for exactly this reason.
ALTER TABLE otel_spans
    ADD COLUMN IF NOT EXISTS AccountIdHash           FixedString(64) DEFAULT '' CODEC(ZSTD(1)),
    ADD COLUMN IF NOT EXISTS AccountIdHashKeyVersion LowCardinality(String);

-- CTO-244 migration: make usage and estimated cost NULLABLE, and teach CostSource to say why.
--
-- WHY. UInt32 and Decimal64(8) have no way to spell "unknown", so ingest was coercing an absent
-- token count or an unpriceable call to 0. The dashboard then read a real, billed call as one that
-- consumed nothing and cost nothing. That breaks the repo's first invariant (an unknown value is
-- null, never 0) and it understates spend in the single most common case there is: a streamed
-- response through the edge proxy, where usage cannot be scanned off the stream.
--
-- This is NOT the additive `ADD COLUMN IF NOT EXISTS` pattern used above. `MODIFY COLUMN` widening
-- a type to its Nullable form is a real mutation: ClickHouse rewrites the affected parts in the
-- background (watch system.mutations). It is safe to replay because modifying a column to the type
-- it already has is a no-op, so `make ch-migrate` stays idempotent. Ingest is not blocked while it
-- runs. Widening UInt32 -> Nullable(UInt32) and Decimal64(8) -> Nullable(Decimal64(8)) loses no
-- value. Extending the CostSource enum keeps 1 and 2 on their existing labels, so already-written
-- rows keep their meaning.
--
-- THE CUTOVER IS AMBIGUOUS AND WE DO NOT PAPER OVER IT. Rows written before this migration hold 0
-- where the truth was EITHER a genuine provider-reported zero OR an unknown that ingest flattened.
-- Nothing recorded which, so nothing can tell them apart now. There is deliberately no backfill:
-- guessing which zeros "should" be NULL would fabricate exactly the kind of number this change
-- exists to remove. Consequence, stated plainly: token and cost figures covering any period before
-- the cutover may UNDERSTATE real spend, and no query can quantify by how much. Post-cutover rows
-- are honest. See RUNNING.md ("Nullable usage and cost").
ALTER TABLE otel_spans
    MODIFY COLUMN InputTokens       Nullable(UInt32)       CODEC(T64, ZSTD(1)),
    MODIFY COLUMN OutputTokens      Nullable(UInt32)       CODEC(T64, ZSTD(1)),
    MODIFY COLUMN CachedInputTokens Nullable(UInt32)       CODEC(T64, ZSTD(1)),
    MODIFY COLUMN EstimatedCost     Nullable(Decimal64(8)) CODEC(ZSTD(1)),
    MODIFY COLUMN CostSource        Enum8('estimated' = 1, 'reconciled' = 2, 'unpriced' = 3);

-- CTO-245 ENGINE MIGRATION: WHAT AN EXISTING INSTALL MUST DO. Read this before trusting the
-- ENGINE line above.
--
-- The CREATE above is `IF NOT EXISTS`, so it only ever takes effect on a FRESH database. A
-- database that already has otel_spans is still on plain MergeTree with the old sorting key, and
-- `make ch-migrate` will NOT change that: ClickHouse cannot ALTER a table's engine or its ORDER BY,
-- so there is no idempotent statement that could be added here to do it. That is a real gap and it
-- is stated rather than papered over. An existing install has NO span deduplication until an
-- operator runs the one-shot migration:
--
--   db/clickhouse/migrations/otel_spans_replacing_engine.sql   (or `make ch-migrate-otel-engine`)
--
-- which creates the correctly-shaped table, copies the data, swaps the names atomically, collapses
-- the historical duplicates and restores the TTL. It is a full copy of the raw span table, so it is
-- deliberately explicit and is not part of the ch-migrate replay set.
--
-- To find out which engine a deployment is ACTUALLY on, ask the database, not this file:
--
--   SELECT engine, sorting_key FROM system.tables
--    WHERE database = currentDatabase() AND name = 'otel_spans';
