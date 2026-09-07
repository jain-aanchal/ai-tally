-- Durable ingest batch idempotency (CTO-245).
--
-- WHY THIS TABLE EXISTS. Batch idempotency was in-process only. `tally.wire.IdempotencyCache` is a
-- dict with a 24h TTL living inside one gateway worker, so the (tenant_id, batch_id) record died
-- with the process. A client that retried a batch across a gateway restart, a deploy, a crash or a
-- scale-out onto a second worker was accepted a second time and its spans were written twice.
--
-- In a cost product that is not a cosmetic duplicate, it is wrong money. `otel_spans` was a plain
-- MergeTree with no dedup of any kind, so the second copy stayed forever and every sum over it was
-- inflated by exactly the replayed spend. One local end-to-end run that re-posted across two
-- gateway restarts left 333,689 rows holding 271,571 distinct SpanIds: about 62,000 spans of
-- permanently double-counted cost.
--
-- WHY POSTGRES AND NOT CLICKHOUSE. The check has to be a decision, not an estimate:
--
--   * It must be CORRECT UNDER CONCURRENCY. Two workers racing the same batch_id must produce
--     exactly one winner. `INSERT ... ON CONFLICT DO NOTHING` against this primary key is atomic
--     and settles that race in one statement. ClickHouse has no unique constraint; a
--     ReplacingMergeTree "check" would be a read of a table that has not merged yet, which is a
--     guess, and guessing is what caused this bug.
--   * It is PER BATCH, not per span, so a single indexed round trip per request is affordable on
--     the ingest hot path. A batch carries up to `max_batch_size` spans (1000 by default), so this
--     is one primary-key lookup amortised over the whole batch.
--   * The gateway already holds a Postgres connection for API-key auth and tenant resolution, so
--     this adds no new dependency and no new failure domain.
--
-- STATE MACHINE, and why `in_flight` is a distinct state rather than a bare presence check.
-- Claiming the key and recording the outcome cannot be one write: the outcome is not known until
-- the spans are written. So a claim inserts `in_flight`, and `record()` promotes it to `complete`
-- with the response body the client replayed for. That gives the racing second submitter an honest
-- answer instead of a wrong one:
--
--   * `complete`  -> the first attempt finished. Return its stored response, marked as a replay.
--     Nothing is re-processed and nothing is re-written.
--   * `in_flight` -> another worker holds this batch RIGHT NOW and we do not yet know whether its
--     spans landed. Answering "accepted" would be a claim we cannot support, and processing the
--     batch ourselves is precisely the double-write this table exists to stop. The gateway returns
--     a retryable response, and the retry gets the recorded outcome.
--
-- WHY THERE ARE TWO EXPIRIES. `first_seen_at` ages the whole record out after the idempotency
-- window (the gateway passes its configured TTL, default 24h): past that horizon a replay is
-- indistinguishable from a new batch and the row is reclaimed rather than kept forever. The
-- `in_flight` LEASE is much shorter and covers a different failure: a worker that is killed between
-- claiming a batch and recording its outcome would otherwise leave the key locked for the entire
-- window, so a legitimate client retry could never be served. After the lease elapses the claim is
-- reclaimable. The lease is therefore a bound on how long a crash can block a retry, and it is the
-- one place where a duplicate remains theoretically possible: a worker that is paused for longer
-- than the lease, and then resumes and writes, can be overlapped by a retry. That residual is the
-- reason `otel_spans` also carries a ReplacingMergeTree backstop (db/clickhouse/otel_spans.sql).
--
-- WHAT IS DELIBERATELY NOT STORED. The `response` column holds the gateway's own BatchResponse
-- envelope: batch_id, status, accepted span COUNT, per-item error codes. No span, no prompt, no
-- completion and no raw identifier ever reaches this table. It is a receipt, not a copy of the
-- payload, and the no-bodies-in-telemetry rule applies to the control plane too.

CREATE TABLE IF NOT EXISTS ingest_batch_idempotency (
    -- The tenant as the gateway resolved it (the API key's tenant when auth is on). TEXT with no FK
    -- to `tenants`, matching scheduler_runs (0027) and tenant_ingest_cursors (0028): a receipt that
    -- outlives its tenant row is inert, whereas an ON DELETE CASCADE firing mid-ingest would drop
    -- the claim out from under a batch in flight and re-open the double-write window.
    tenant_id     TEXT NOT NULL CHECK (length(tenant_id) > 0),
    -- The client-generated batch id (UUIDv7 from the SDK). Bounded because it is an opaque handle,
    -- not a description, and an unbounded client-supplied key is an index-bloat vector.
    batch_id      TEXT NOT NULL CHECK (length(batch_id) > 0 AND length(batch_id) <= 200),
    state         TEXT NOT NULL CHECK (state IN ('in_flight', 'complete')),
    -- The recorded BatchResponse, replayed verbatim to a duplicate submit. NULL while in_flight,
    -- which is exactly the "we do not know yet" state and must never be rendered as success.
    response      JSONB,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, batch_id)
);

-- Supports the periodic prune. The claim path never scans this index (it is a primary-key lookup);
-- this exists so deleting aged-out receipts stays a bounded range delete rather than a seq scan of
-- the whole table, which on a busy tenant is the difference between a background tidy and an
-- ingest-blocking vacuum storm.
CREATE INDEX IF NOT EXISTS ingest_batch_idempotency_first_seen_idx
    ON ingest_batch_idempotency (first_seen_at);
