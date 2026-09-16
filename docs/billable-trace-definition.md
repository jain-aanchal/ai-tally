# What a billable trace is (CTO-403)

Written after CTO-396 (#406) changed what `/v1/usage` counts for SDK traffic without saying so in
its description. This states the unit the invoice is computed from, path by path, says where today's
code bills something the definition would not endorse, and records a measured before and after.

It describes the ClickHouse-side number only. Whether that number or the in-process head meter is
the bill at all is CTO-399, and the head meter's treatment of synthetic trace ids is CTO-401.
Neither is decided here.

## The rule as the code has it

`GET /v1/usage` answers from `ClickHouseStore.usage_counts`
(`infra/gateway/src/gateway/store.py`, CTO-390):

```sql
SELECT uniqExactIf(TraceId, notEmpty(TraceId)),
       uniqExactIf(FeatureTag, notEmpty(FeatureTag))
FROM otel_spans
WHERE TenantId = %(t)s AND Timestamp >= %(s)s AND Timestamp < %(e)s
```

So, literally:

> A billable trace is one distinct non-empty `TraceId` among the spans stored for that tenant whose
> `Timestamp` falls in the billing period.

Three consequences follow from the literal rule and are worth stating because each is load-bearing:

1. **Stored, not sent.** A span the gateway never wrote is not billed. On the synchronous ingest
   path stored and accepted are the same set; on the buffered path (`TALLY_INGEST_BUFFERED=true`,
   off everywhere today) they are not, which is the CTO-399 gap.
2. **Whoever sets `TraceId` sets the unit.** The gateway does not group spans itself. It counts the
   ids the client put on the wire, and `gateway.mapping.span_to_row` invents a fresh random id per
   row for any span that arrives without one.
3. **The period window is per span, not per trace.** A trace whose spans straddle midnight on the
   first of a month contributes a billable trace to both months.

## What one unit means on each ingest path

### Edge proxy

`infra/edge-proxy/internal/telemetry/telemetry.go` builds one span per intercepted request, stamps
a fresh random `trace_id` and `span_id` on it, and posts it as a single-span batch. One billable
trace is therefore **one metered LLM request**. The proxy has no notion of a user journey, so it
never joins two requests into one trace.

This is a coherent unit, and it is the unit almost all real metered traffic is billed on today. It
is not the same unit as the SDK's traced path, which is the problem below.

### SDK with an explicit `start_trace`

`TallyClient._emit` stamps every span with the trace id from the caller's context
(`sdk/python/src/tally/client.py`, `_with_span_ids`, CTO-396), so all spans inside one `start_trace`
share one id. One billable trace is **one `start_trace` scope**: one user journey, however many
model calls, tool calls and retrievals it contains.

This is the unit the product's language implies and the one this document endorses.

### SDK with no `start_trace`

A span emitted outside a trace gets its own fresh trace id, so one billable trace is **one span**.

The definition does not endorse this. A per-span charge is being counted under the name of a trace,
which means the same five model calls cost five units when the caller did not open a trace and one
unit when they did. Nothing tells the customer that wrapping their code in `start_trace` cuts their
bill by a factor of five, and nothing marks these ids as synthetic, so no downstream reader can tell
them from real traces. CTO-401 is deciding what the head meter should do with them. The two choices
there reach the invoice differently:

- Leave the trace id empty for a trace-less span: `notEmpty(TraceId)` then excludes those spans from
  the invoice entirely, and a tenant who never calls `start_trace` bills zero traces while still
  costing storage and query.
- Keep the id but mark it synthetic: the invoice query has to learn about the marking, or it goes on
  billing one per span.

Either way the invoice moves, so CTO-401 and this definition have to be settled together.

### Any other id-less client

A client that posts spans with no ids at all (a curl, a third-party exporter, a homegrown script)
gets `mapping.span_to_row`'s random per-row fallback, so it bills one trace per span, the same as
the trace-less SDK path and for the same reason. This fallback is what SDK traffic relied on before
CTO-396.

### OTLP

`/v1/otlp/traces` copies `traceId` and `spanId` straight off the OTLP span
(`gateway.protocol.otlp_traces_to_spans`), so one billable trace is **one trace as the customer's
own tracer defined it**. That is the most honest of the four, since the customer chose the
boundaries, and it is also the least under our control: a tenant whose framework opens one long
trace per worker process bills one trace for a day of work.

### Summary

| Path | One billable trace is | Endorsed |
|---|---|---|
| Edge proxy | one intercepted LLM request | Yes, but it is not the SDK's unit |
| SDK, inside `start_trace` | one `start_trace` scope | Yes |
| SDK, no `start_trace` | one span | No, see CTO-401 |
| Other id-less client | one span (mapper fallback) | No, same reason |
| OTLP | one caller-defined OTel trace | Yes |

## What the definition would not endorse, plainly

- **The unit is not stable across ingest paths.** One user journey of five model calls bills as one
  trace through the SDK with a trace open, and as five through the edge proxy. Same workload, same
  customer, five times the bill depending on how they integrated. Nothing in the pricing material
  says which integration a quoted price assumes.
- **A trace-less span is billed as a trace.** Described above.
- **A trace that crosses a period boundary is billed in both periods.** Defensible for a metered
  month, but it is a rule nobody has written down, and it means a long-running agent run is charged
  twice.
- **Re-ingest of id-less spans re-bills them.** Batch idempotency (`gateway.batch_idempotency`)
  catches a replayed batch by batch id, and a genuine duplicate span with real ids collapses in
  `BatchRequest.deduplicated()`. A client that re-sends the same work as a fresh id-less batch gets
  fresh random ids per row and a fresh charge. The protection is at the batch level, not at the
  work level.

## Measured before and after

**What this is.** A fixed synthetic workload put through the real SDK span-build path and the real
`/v1/batches` handler with a fake store, at two commits: `a010a15` (the merge before CTO-396) and
`768c77e` (main, after it). `billable_traces` is computed exactly as the invoice query computes it,
as the count of distinct non-empty `TraceId` among the rows that reached the store.

**What this is not.** It is not a real tenant-month. I have no production access, and the local
stack holds no SDK-originated spans at all: every row in it came from `make seed` or from
`examples/vercel-chatbot/scripts/backfill-spans.ts`, a synthetic generator that stamps its own ids
per span the way the edge proxy does. So the before and after below is a measurement of the code's
behaviour, not of anyone's invoice.

Workload: 100 journeys of 5 `record_llm_call`s each, 500 spans in total.

| Workload | Before (`a010a15`) | After (`768c77e`) |
|---|---|---|
| Traced, 5 spans per batch | 100 rows stored, **100** billable | 500 rows stored, **100** billable |
| Trace-less, 5 spans per batch | 100 rows stored, **100** billable | 500 rows stored, **500** billable |
| Traced, 1 span per batch | 500 rows stored, **500** billable | 500 rows stored, **100** billable |

Read the rows rather than the headline. The direction of the invoice change depends on the batch
shape, which depends on how many spans a customer's app produced inside one flush interval:

- **Multi-span batches, traced (row 1).** The count did not move. Before CTO-396 the batch collapsed
  to one stored row carrying one random fallback id, which coincidentally equals the one real trace
  id it bills as now. What changed is that 400 of the 500 spans, and the spend on them, were being
  thrown away. The bill was right by accident on top of data that was wrong.
- **Multi-span batches, trace-less (row 2).** Five times higher, because the spans that were being
  dropped are now stored and each carries its own id.
- **One span per batch, traced (row 3).** Five times lower. Nothing was being collapsed here, so
  every span used to bill as its own trace under the mapper's fallback id, and five of them now
  share one real id.

CTO-403 states the traced case as a five-fold fall. That is true for row 3 and not for row 1, so
the real exposure for any given tenant depends on their flush behaviour and cannot be settled from
the code alone. It needs the stored data for that tenant.

**The shape the only available corpus has.** For orientation only, the local ClickHouse holds a
30-day synthetic corpus for the demo tenant at about 4.2 spans per trace over a month (278622 rows
against 66445 distinct trace ids for 2026-08). If a real SDK tenant's traffic has that shape, and if
it flushes one span per batch, row 3 is the case that applies and their billable count falls by
about 4 times. Both conditions are assumptions, not observations.

**To settle it on real data**, run per tenant-month against production ClickHouse:

```sql
SELECT TenantId,
       count()                                    AS spans,
       uniqExactIf(TraceId, notEmpty(TraceId))    AS billable_traces_today,
       round(count() / uniqExactIf(TraceId, notEmpty(TraceId)), 2) AS spans_per_trace
FROM otel_spans
WHERE Timestamp >= '2026-09-01' AND Timestamp < '2026-10-01'
GROUP BY TenantId
ORDER BY spans DESC
```

For a tenant on the SDK, `spans_per_trace` is roughly the factor by which the billable count fell,
and `spans` is roughly what the old per-row behaviour would have billed. For a tenant on the edge
proxy, `spans_per_trace` is 1 and nothing changed.

## Pricing consequence, for the owner to decide

CTO-21 settled a pricing model, and CTO-288 and CTO-304 build plan limits and overage on top of the
count this document describes. `DEFAULT_PLAN_LIMIT` in `gateway/metering.py` is 100,000 traces on
free.

What the owner has to decide, not what this change decides:

1. **Which unit the price is quoted against.** As long as a proxy request and an SDK journey are
   both called a trace, the same workload has two prices. Either the unit is per request everywhere
   and the SDK's traced grouping stops being the billing unit, or the unit is per journey and the
   proxy needs a way to join requests into one.
2. **Whether any limit or quoted figure was sized against the old per-row behaviour.** Both figures
   above came from the same query and the same table, so nothing in the repo records what a pilot
   was quoted. If a free-tier ceiling or a pilot number was picked by looking at `/v1/usage` before
   2026-09-16, it was measuring per-row counts for SDK tenants and is now measuring per-trace ones.
3. **Whether the fall for existing SDK tenants is applied or absorbed.** A tenant whose count fell
   is being undercharged relative to the old basis and overcharged relative to nothing: the number
   they see is the honest one under this definition. Moving them back up would require a decision to
   bill per span, which is decision 1 again.

The tests in `infra/gateway/tests/test_billable_trace_definition.py` pin what is written here, so a
future change to `TraceId` semantics fails a test rather than moving an invoice quietly.
