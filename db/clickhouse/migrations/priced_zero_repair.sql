-- CTO-313: repair historical spans stored as a priced $0 that was never a measurement.
-- EXPLICIT, one-shot, operator-run. Not part of `make ch-migrate`. Run `make ch-repair-priced-zero`.
--
-- RUN `make ch-priced-zero-check` FIRST and read db/clickhouse/checks/priced_zero.sql. That file
-- carries the full definition of the four classes below and why each is decided the way it is; this
-- one only acts on them. This script mutates the raw span table.
--
-- ############################################################################################
-- # THE DECISION, PER POPULATION                                                              #
-- ############################################################################################
--
-- CTO-244 stopped NEW spans asserting a fabricated $0 but did not rewrite the rows already written
-- that way, so a dashboard over history still shows measured-looking zeros. The issue asked whether
-- to backfill, mark, or document the cutover. The answer differs per population, and only one of
-- the three options is available for any given row:
--
--   1. REPRICE where the true cost survives. Tool spans written before the gateway promoted
--      `gen_ai.tool.cost_micro_usd` into EstimatedCost still carry that attribute. The money is in
--      the row. Step 1 promotes it with the same arithmetic the gateway uses, and the check file
--      verifies that arithmetic against the 183k+ spans the gateway itself already promoted, where
--      attribute and column must agree exactly. This is a repricing, not an estimate.
--
--   2. MARK UNKNOWN where it does not. Two shapes, both structural:
--        a. the span records nothing that could ever have been priced (no model either side, no
--           tokens, no client-reported cost), so the stored 0 is a column default, not a result;
--        b. PriceCatalogVersion is '', the empty-version signal tally.pricing returns on a catalog
--           miss, so the catalog never produced a price and ingest wrote 0 anyway. These rows often
--           carry a real model and real token counts, which makes their $0.00 the most misleading
--           figure in the table.
--      They become EstimatedCost = NULL, CostSource = 'unpriced', which is exactly what a
--      post-CTO-244 gateway writes for the same situation, so the dashboard renders them as a blank
--      with a reason instead of a confident zero. No value is invented for them, now or ever: what
--      the call really cost is not recorded anywhere and cannot be recovered.
--
--   3. DOCUMENT, for the zeros that are real. A span with a genuine PriceCatalogVersion AND a model
--      to have priced was priced, and the answer was zero. CTO-244 was explicit that a real 0 stays
--      0 and stays distinguishable from NULL. Turning those into unknowns would destroy information,
--      which is the same sin in the other direction, so this script does not touch them.
--
-- WHY NOT A CUTOVER DATE, which the issue offered as an option. Nothing in otel_spans records when a
-- row was WRITTEN. Timestamp is the span's own time and the demo backfill posts spans backdated 30
-- days, so a date comparison would mark freshly-written rows as historical and miss backdated ones.
-- Every predicate below is therefore structural: it reads what the row itself carries. That is
-- exact, needs no operator-supplied date, and is correct on a stack whose history was backfilled.
--
-- ############################################################################################
-- # BEFORE YOU RUN IT                                                                         #
-- ############################################################################################
--
--   * ALTER ... UPDATE is a ClickHouse mutation: it rewrites the affected parts in the background.
--     `mutations_sync = 2` below makes each statement wait for completion so the verification at
--     the end is meaningful. Watch system.mutations if one appears to hang.
--   * THE ROLLUPS DO NOT FOLLOW. Materialized views fire on INSERT, never on a mutation, so every
--     dollar this script changes leaves daily_feature_rollup, hourly_feature_rollup and
--     daily_account_rollup holding the OLD money. The dashboard reads rollups. Run
--     `make ch-rollup-rebuild` (CTO-311) AFTERWARDS, in that order, or the repair is invisible where
--     it matters and the raw table and the rollups disagree. RUNNING.md sequences this.
--   * There is no shadow-table rollback here, because a mutation is not a swap. Snapshot first if
--     this is not a local stack. What the script does give you is a before-and-after ledger of every
--     row it touched, so the change to any historical total is explicable rather than mysterious.
--   * Quiescing ingest is not required. Every predicate is structural, and a span the gateway writes
--     while this runs is already honest.
--
-- Money is integer micro-USD throughout. The conversion to the Decimal64(8) USD column happens once,
-- at the single boundary where the attribute becomes the column, and nowhere else.

-- ============================================================================================
-- 0. BEFORE. Recorded first so the ledger in step 3 has something to be compared against, and so a
--    run that is interrupted still leaves the operator with the starting numbers.
-- ============================================================================================
SELECT
    'before' AS phase,
    countIf(EstimatedCost = 0) AS priced_zero_spans,
    countIf(EstimatedCost IS NULL) AS unknown_spans,
    countIf(EstimatedCost = 0 AND toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']) IS NOT NULL)
        AS repriceable_spans,
    sumIf(toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']),
          EstimatedCost = 0 AND toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']) IS NOT NULL)
        AS recoverable_micro_usd,
    sum(EstimatedCost) AS known_spend_usd
FROM otel_spans FINAL
FORMAT Vertical;

-- ============================================================================================
-- 1. REPRICE the spans whose true cost survives on the row.
--
--    Scoped to EstimatedCost = 0 so a span the gateway already promoted is never rewritten, and to
--    toInt64OrNull(...) IS NOT NULL so a malformed attribute is left for step 2 rather than being
--    coerced to a zero by toInt64OrZero, which would manufacture the exact defect being repaired.
--
--    PriceCatalogVersion is deliberately NOT set. This cost came from the client, not from a price
--    catalog, and the empty version is the honest record of that. It is the same version the
--    gateway leaves on the spans it promotes today, so repaired rows and new rows agree.
-- ============================================================================================
ALTER TABLE otel_spans
UPDATE
    EstimatedCost = toDecimal64(toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']), 8) / 1000000,
    CostSource = 'estimated'
WHERE EstimatedCost = 0
  AND toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']) IS NOT NULL
SETTINGS mutations_sync = 2;

-- ============================================================================================
-- 2. MARK THE REST UNKNOWN. Runs strictly after step 1, and the ordering is load-bearing: a
--    repriceable span is no longer 0 by the time this predicate is evaluated, so it cannot be
--    caught here and have its recovered money thrown away again.
--
--    The two limbs are the (a) and (b) shapes described at the top. The attribute exclusion is a
--    belt-and-braces guard for a client that genuinely reported a cost of zero: that is a
--    measurement and it stays 0, even though limb (b) would otherwise match it.
-- ============================================================================================
ALTER TABLE otel_spans
UPDATE
    EstimatedCost = NULL,
    CostSource = 'unpriced',
    PriceCatalogVersion = ''
WHERE EstimatedCost = 0
  AND SpanAttributes['gen_ai.tool.cost_micro_usd'] = ''
  AND (
        (GenAiRequestModel = '' AND GenAiResponseModel = ''
         AND ifNull(InputTokens, 0) = 0 AND ifNull(OutputTokens, 0) = 0)
        OR PriceCatalogVersion = ''
      )
SETTINGS mutations_sync = 2;

-- ============================================================================================
-- 3. AFTER, and the ledger. `known_spend_usd` moving UP is the recovered tool spend; it must move
--    up by exactly recoverable_micro_usd from step 0, because marking a row unknown removes a zero
--    from the sum and a zero changes no total. That is the arithmetic proof that the mark step
--    subtracted no money: it only stopped claiming that money was known.
--
--    priced_zero_spans should now be only the genuine measured zeros (usually none), and
--    unknown_spans should have grown by exactly the number of rows step 2 marked.
-- ============================================================================================
SELECT
    'after' AS phase,
    countIf(EstimatedCost = 0) AS priced_zero_spans,
    countIf(EstimatedCost IS NULL) AS unknown_spans,
    countIf(EstimatedCost = 0 AND toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']) IS NOT NULL)
        AS repriceable_spans,
    sum(EstimatedCost) AS known_spend_usd
FROM otel_spans FINAL
FORMAT Vertical;

SELECT
    TenantId,
    GenAiOperation,
    CostSource,
    count() AS spans,
    sum(EstimatedCost) AS known_spend_usd,
    countIf(EstimatedCost IS NULL) AS unknown_spans,
    min(toDate(Timestamp)) AS first_day,
    max(toDate(Timestamp)) AS last_day
FROM otel_spans FINAL
GROUP BY TenantId, GenAiOperation, CostSource
ORDER BY TenantId, spans DESC
FORMAT PrettyCompact;

-- ============================================================================================
-- 4. NOW REBUILD THE ROLLUPS. Not optional, and not something this script can do for you: the
--    materialized views did not see any of the mutations above, so every rollup still holds the
--    pre-repair money and the dashboard, which reads rollups, still shows it.
--
--   make ch-rollup-rebuild   # from infra/, CTO-311, quiesce ingest for the duration
--
--    Then `make ch-priced-zero-check` again. The unknown_* classes should be gone, and whatever
--    remains under `measured_zero` is a real zero that was correctly left alone.
-- ============================================================================================
