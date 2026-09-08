-- CTO-313: which historical spans assert a priced $0 that is not a measurement?
-- READ-ONLY. Run it with `make ch-priced-zero-check` from infra/. It writes nothing.
--
-- WHY THIS EXISTS. CTO-244 made EstimatedCost nullable so a span we cannot price stores NULL with
-- CostSource = 'unpriced' and renders as a blank with a reason. That stopped NEW spans asserting a
-- fabricated $0. It did not rewrite the rows already written that way, and those rows are still
-- what a dashboard over historical data reads: a confident, measured-looking $0.00 on calls that
-- either cost real money or cost an amount nobody ever established.
--
-- HOW A FABRICATED ZERO IS TOLD APART FROM A REAL ONE, without a cutover date.
--
-- The tempting rule is "everything before the CTO-244 cutover". It does not work here, and saying
-- why matters more than the rule. Nothing in otel_spans records when a row was WRITTEN: Timestamp
-- is the span's own time, and the demo backfill posts spans backdated 30 days. A cutover date
-- compared against Timestamp would therefore mark freshly-written rows as historical and miss
-- backdated ones. So this classifies on what the row itself carries, which is exact and needs no
-- date at all:
--
--   recoverable        cost is 0, PriceCatalogVersion is '', and the span still carries the ORIGINAL
--                      client-reported cost in SpanAttributes['gen_ai.tool.cost_micro_usd']. The
--                      gateway now promotes that attribute into EstimatedCost; these rows predate
--                      the promotion. The money is right there, so this is a repricing, not a guess.
--                      The test is toInt64OrNull(...) IS NOT NULL rather than "the key is present",
--                      on purpose: toInt64OrZero on a malformed value would manufacture the very
--                      zero this is trying to remove, so an unparseable attribute falls through to a
--                      class below and is treated as unknown.
--                      THE EMPTY-VERSION REQUIREMENT IS LOAD-BEARING, and it used to be missing.
--                      gen_ai.tool.cost_micro_usd is not promoted out of SpanAttributes (it is
--                      absent from mapping.py's _PROMOTED_GENAI), so it survives on the row next to
--                      a cost the price catalog computed. A tool or vector span the catalog
--                      legitimately prices at zero therefore carries a REAL version and the client
--                      hint at once. Because this branch is evaluated first, such a row was never
--                      even reported as measured_zero, and the repair would have overwritten a
--                      catalog-authoritative zero with a client hint. An empty version is the only
--                      state in which no catalog ever spoke and the hint is the best evidence there
--                      is.
--
--   unknown_no_price_input
--                      cost is 0 and the span records NOTHING that could ever have been priced: no
--                      request model, no response model, no tokens, no client-reported cost. A
--                      price catalog cannot produce a number from that, so the stored 0 is a column
--                      default, not a result. (These rows can still carry a PriceCatalogVersion,
--                      which is what makes them look measured and is exactly the trap.)
--
--   unknown_catalog_miss
--                      cost is 0 and PriceCatalogVersion is '', which is the empty-version signal
--                      tally.pricing already returns on a catalog miss (see the CTO-244 note in
--                      otel_spans.sql). The catalog never produced a price; ingest wrote 0 anyway.
--                      Rows here usually DO carry a model and real token counts, so the money is
--                      real and unknown, which is the worst of the three to report as $0.00.
--
--   measured_zero      cost is 0, a real price catalog version is stamped, there is a model to have
--                      priced AND there are tokens to have priced it on. This is a provider-reported
--                      or catalog-computed zero and it is a fact. It is listed so it is visibly
--                      EXCLUDED from any repair. CTO-244 was explicit that a real 0 stays 0 and
--                      stays distinguishable from NULL; turning these into unknowns would destroy
--                      information, which is the same sin in the opposite direction.
--
--   measured_zero_no_model
--   measured_zero_flat_usage
--                      the residue, split out rather than folded into measured_zero, because the
--                      class doc used to promise "a real model, real tokens and a real catalog
--                      version" while the SQL asked only for a version plus (a model OR non-zero
--                      tokens). Two populations slipped through that gap and sat inside a class name
--                      claiming more certainty than it had: rows with tokens but no model on either
--                      side, which enrich_cost can never price, and pre-CTO-244 flattened-usage rows
--                      with a model, a real version and tokens coerced to 0, which is CTO-244's own
--                      motivating case. Both keep a real PriceCatalogVersion, so a catalog did
--                      produce that number and the repair leaves them alone exactly as it leaves
--                      measured_zero alone. Naming them is the point: this is the residue that
--                      neither the repair nor this check can decide, and it should be visible rather
--                      than laundered into a confident label.
--
-- The classes are evaluated in that order and are mutually exclusive, so the counts partition the
-- priced-zero population exactly.
--
-- Money is integer micro-USD. `recoverable_micro_usd` is summed straight off the attribute with no
-- conversion at all; the Decimal64(8) USD column is only computed for display.

SELECT
    TenantId,
    GenAiOperation,
    multiIf(
        PriceCatalogVersion = ''
            AND toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']) IS NOT NULL, 'recoverable',
        GenAiRequestModel = '' AND GenAiResponseModel = ''
            AND ifNull(InputTokens, 0) = 0 AND ifNull(OutputTokens, 0) = 0, 'unknown_no_price_input',
        PriceCatalogVersion = '', 'unknown_catalog_miss',
        GenAiRequestModel = '' AND GenAiResponseModel = '', 'measured_zero_no_model',
        ifNull(InputTokens, 0) = 0 AND ifNull(OutputTokens, 0) = 0, 'measured_zero_flat_usage',
        'measured_zero'
    ) AS class,
    count() AS spans,
    min(toDate(Timestamp)) AS first_day,
    max(toDate(Timestamp)) AS last_day,
    -- Only the recoverable class has a number to report. The other classes are unknown, and an
    -- unknown does not get a total: summing them would produce the same confident 0.00 this check
    -- exists to expose. They read as a blank here for the same reason the dashboard blanks them.
    if(class = 'recoverable',
       toString(sum(toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']))),
       '') AS recoverable_micro_usd,
    -- Divided BEFORE widening. `toDecimal64(micro, 8)` widens the micro-USD integer to a scale-8
    -- Decimal64, which leaves ten integer digits and throws above about 92,233,720,368 micro-USD,
    -- roughly $92,233 on one span. One client sending nanos would hard-fail a READ-ONLY check,
    -- which is the last thing a check should do. divideDecimal converts at scale 0 instead.
    if(class = 'recoverable',
       toString(divideDecimal(sum(toDecimal64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd'], 0)),
                              toDecimal64(1000000, 0), 8)),
       '') AS recoverable_usd
FROM otel_spans FINAL
WHERE EstimatedCost = 0
GROUP BY TenantId, GenAiOperation, class
ORDER BY spans DESC
FORMAT PrettyCompact;

-- Confidence that the repricing formula is the gateway's own, not one invented here. Every span the
-- gateway ALREADY promoted still carries the source attribute alongside the promoted cost, so the
-- conversion can be replayed against them and checked. `conversion_mismatches` must be 0; if it is
-- not, the attribute and the column disagree about what the gateway does and the reprice in
-- db/clickhouse/migrations/priced_zero_repair.sql must not be run until that is understood.
--
-- SCOPED TO PriceCatalogVersion = '', which is what "the gateway promoted this" actually means. The
-- attribute is not promoted out of SpanAttributes, so it also survives on tool and vector spans the
-- price CATALOG costed, where EstimatedCost is the server's price and the attribute is only a client
-- hint that may legitimately differ. Without this scope every such row with any drift at all counts
-- as a mismatch, and the lines above then tell an operator the repair must not be run, on a
-- deployment where nothing is wrong. That is a false block on the only remedy, so it is fixed rather
-- than explained.
SELECT
    count() AS gateway_promoted_spans,
    countIf(EstimatedCost != divideDecimal(toDecimal64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd'], 0),
                                           toDecimal64(1000000, 0), 8))
        AS conversion_mismatches,
    -- Reported separately because a NULL comparison is neither true nor false, so an unparseable
    -- attribute would sit silently outside conversion_mismatches rather than being flagged by it.
    countIf(toInt64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd']) IS NULL) AS unparseable_attrs,
    sum(EstimatedCost) AS stored_usd,
    divideDecimal(sum(toDecimal64OrNull(SpanAttributes['gen_ai.tool.cost_micro_usd'], 0)),
                  toDecimal64(1000000, 0), 8) AS replayed_usd
FROM otel_spans FINAL
WHERE SpanAttributes['gen_ai.tool.cost_micro_usd'] != ''
  AND EstimatedCost != 0
  AND PriceCatalogVersion = ''
FORMAT Vertical;

-- Already-honest rows, for contrast: spans that correctly say "unknown" rather than "$0.00". A
-- deployment that has been repaired should see the unknown_* classes above collapse into this.
SELECT
    TenantId,
    CostSource,
    count() AS spans,
    min(toDate(Timestamp)) AS first_day,
    max(toDate(Timestamp)) AS last_day
FROM otel_spans FINAL
WHERE EstimatedCost IS NULL
GROUP BY TenantId, CostSource
ORDER BY spans DESC
FORMAT PrettyCompact;
