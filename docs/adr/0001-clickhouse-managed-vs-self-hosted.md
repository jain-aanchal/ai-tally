# ADR 0001 — ClickHouse: managed (Cloud) vs. self-hosted

**Status:** Proposed — pending verification of multi-tenant isolation knobs (see §6).
**Date:** 2026-05-28
**Tracks:** CTO-94 spike. Blocks: CTO-22 cluster bring-up, CTO-30 isolation, CTO-29 tiering.
**Decision-maker:** founder / eng lead.

## 1. Context

ai-tally's telemetry store is ClickHouse (spec §5). Shared multi-tenant (CTO-18) + cloud-only
MVP (CTO-19) + self-serve GTM (CTO-82). We need to stand up a cluster *before* customer data
lands, and the schema (CTO-22, CTO-24/25/26) is already written. The question is whether to run
ClickHouse ourselves or pay ClickHouse Cloud.

The blast-radius of the wrong call:

- **Self-host too early** → weeks of founder/eng time on cluster ops (backups, upgrades, sharding,
  on-call) instead of the cost workflows that win or lose deals.
- **Managed forever** → margin tax at scale, and possibly a hard ceiling if the managed offering
  lacks the per-tenant isolation knobs CTO-30 mandates.

Schema portability is a non-issue: managed and self-hosted run the same engine, the same SQL, and
the same DDL we already wrote. Migrating off managed later is real but not a rewrite.

## 2. Options considered

### A. ClickHouse Cloud (managed)

- Hosted, multi-tier offering with on-demand compute and S3-backed storage.
- Operationally: backups, upgrades, replication, monitoring all handled.
- Adds a control-plane dependency we don't control (incidents on their side become ours).
- Per-tenant isolation: **needs verification** — see §6.

### B. Self-hosted on cloud VMs

- We run the cluster (likely k8s + ClickHouse operator, or VMs + clickhouse-keeper).
- Full control over Resource Groups, settings, storage tiers, network policies.
- Operational cost from day one: at least one engineer materially on-call.

### C. Tinybird

- Higher-level abstraction over ClickHouse.
- Excellent DX for query endpoints.
- Adds an opinion layer we have to fight: our design relies heavily on materialized views and
  per-tenant Resource Groups, neither of which Tinybird exposes the same way.
- **Decision: do not use as system of record.** Possibly an API-accelerator later.

### D. Hybrid

- Managed for hot tier, self-hosted for cold/archive.
- Two systems, two on-calls. Defer until we have either the scale or the compliance forcing function.

## 3. Decision

**Start on ClickHouse Cloud (Option A), conditional on verification (§6).** Self-host only when
both conditions hold:

1. Managed cost is a visible % of COGS (rule of thumb: when monthly bill exceeds ~$30k *or*
   exceeds 20% of revenue — whichever comes first).
2. We have at least one engineer with bandwidth to run it well, and we have a real customer asking
   for something managed can't provide (typically dedicated isolation or data residency in a
   region the managed offering doesn't cover).

Until then, **buy time, not infrastructure.**

## 4. Rationale

- **Time-to-first-customer is the only metric that matters right now.** The Phase-1 demo
  (Workflow 4) takes weeks. The fastest path to a running cluster with backups, replication, and
  point-in-time recovery is "click a button on ClickHouse Cloud." Anything else trades demo
  velocity for COGS we don't pay yet.
- **DDL is portable.** Everything in `db/clickhouse/*.sql` (CTO-22/24/25/26) runs unmodified on
  both. No vendor lock-in at the schema layer.
- **Migration off managed is a known operation.** ClickHouse provides `clickhouse-copier` and
  S3-based snapshots. Painful, but bounded and well-trodden.
- **The integrated data spine (traces + business events + identity graph) is the moat.** Self-
  hosting the underlying ClickHouse isn't.

## 5. Cost model (illustrative, not a quote)

Three traffic tiers, modelled against the **Decimal64(8)** cost + bloom-indexed schema from
CTO-22. Numbers are order-of-magnitude estimates from public pricing; actuals must be re-tested
with a real ingest sample.

| Tier | Spans/month | Compressed storage / mo (90d hot retention) | Notional managed cost / mo | Notional self-host (VM + people) |
|---|---|---|---|---|
| **MVP** (5 design partners) | ~5M | ~200 MB | ~$200–400 (smallest tier) | $400 infra + 0.25 FTE = $5k effective |
| **Series A** | ~50M | ~3 GB | ~$1.5k–3k | $1.2k infra + 0.5 FTE = $10k effective |
| **Series B** | ~500M (→ 2B with child spans, per spec) | ~150 GB | ~$15k–30k | $10k infra + 1.0 FTE = $24k effective |

**Assumptions** (each independently flag-and-revisit):
- Average compressed span size 1.5–2 KB after the codecs we specified (Delta+ZSTD on ts, T64 on
  tokens, ZSTD elsewhere). Real data may be larger or smaller; verify with a 1M-row sample.
- 90-day hot retention; ≥90d aggregated cold lives in the rollup MVs (CTO-24) which compress an
  additional ~10×.
- "FTE" cost for self-hosting captures pager rotation, upgrades, capacity planning, incident
  response. The big-org rule of thumb is 0.25 → 1.0 SREs per cluster as load grows.

**Crossover point**: managed beats self-host by COGS roughly **until** the Series-B tier, where
the lines start crossing if you don't account for engineering opportunity cost. Once you do
account for it, the crossover moves later. **Translate**: don't self-host until you're ready to
hire for it.

## 6. Verification checklist — **MUST do before locking in**

These are the questions that would force Option B if any answer is "no":

| # | Question | Why it matters | How to verify |
|---|---|---|---|
| 1 | Does ClickHouse Cloud expose **per-tenant query quotas** (settings: `max_concurrent_queries_for_user`, `max_memory_usage_for_user`, query timeouts)? | CTO-30 requires per-tenant concurrency cap and memory cap. Without these the noisy-neighbor problem from §3.2 of the spec stress test is acute. | Read [Cloud settings docs](https://clickhouse.com/docs/en/cloud); open a support ticket if unclear. Confirm settings can be applied at user/role level. |
| 2 | Can we create **per-tenant database users/roles** with row-level policies scoped by `TenantId`? | Defense-in-depth on top of app-layer tenant scoping. Required for SOC 2 (CTO-16). | Documented + tested on a free tier instance. |
| 3 | What is the **realistic isolation** between tenants in the same Cloud service? CPU/memory share or hard partition? | If "share," we need to model the worst-case neighbour. | Their docs + support; benchmark if necessary. |
| 4 | Are **materialized views** (incl. `SummingMergeTree` and `ReplacingMergeTree` targets, CTO-24/25) fully supported, including the `AggregateFunction` columns? | The rollup pipeline is the read path; CI lint rejects raw-span queries. | Apply our DDL to a Cloud sandbox and verify MVs populate on insert. |
| 5 | Are **storage-tier policies** (volume `'warm'`, `INTERVAL N DAY TO VOLUME 'warm'`) configurable, or does Cloud manage hot/cold transparently? | CTO-29 / CTO-22 TTL refers to a `warm` volume. If Cloud abstracts this, our TTL clause must be rewritten. | Try the DDL; check Cloud docs for storage policies. |
| 6 | What's the **point-in-time recovery RPO/RTO**? CTO-14.6 wants 4h RTO / 1h RPO. | Compliance gap if PITR weaker than spec. | Documented on the Cloud SLA page; confirm the tier required. |
| 7 | Can we run **HMAC-keyed `DELETE` mutations** on demand for right-to-deletion (CTO-76)? Is there a quota? | GDPR / CCPA 30-day SLA. | Run a `DELETE` on a sandbox dataset, time it. |
| 8 | Data residency: do they offer regions matching our target ICPs (US, EU at minimum)? | CTO-76; deal-breaker for EU customers. | Their public region list. |

**If any 1, 2, 4, or 5 is "no," default to self-hosted from day one.** The others have workarounds.

## 7. Triggers to revisit (move to self-hosted)

Any one of:

- Monthly managed bill crosses $30k *or* 20% of MRR.
- An enterprise customer specifically requires dedicated single-tenant hosting.
- A region we don't currently support becomes a deal-blocker for ≥2 prospects.
- We hit a managed-platform limit on materialized-view count, partitions, or quota.
- We've hired a dedicated infra/SRE who has the capacity to own the cluster.

## 8. Consequences

**Positive:**
- Phase 1 unblocks now (weeks, not month-plus, for a production-grade cluster).
- Engineering time goes into the cost workflows and the integrated data spine.
- Backups, replication, version upgrades — not our problem yet.

**Negative:**
- A line item on COGS we can't fully optimize.
- A control-plane outside our control; their incident is ours.
- A future migration project (bounded, documented, but real).

**Neutral:**
- Schema and code identical either way. No long-term lock-in.

## 9. Action items (immediate)

- [ ] Spin up a free / smallest-tier Cloud instance.
- [ ] Apply `db/clickhouse/*.sql` (otel_spans, rollups, last_touch_index, attribution).
- [ ] Verify checklist §6 #1–#8 against that instance + the docs.
- [ ] Ingest a 1M-row synthetic sample (we have the schema; generate from `tally.schema` + price catalog) and measure compressed size vs. our assumption.
- [ ] Make the call: managed (recommended) or pivot to self-hosted.
- [ ] Update CTO-22 status from blocked → ready.

## 10. References

- ai-tally System Specification §5 (storage), §11 (multi-tenancy), §14 (security).
- ai-tally PR #8 (otel_spans DDL), PR #17 (rollups + attribution DDL), PR #18 (Postgres
  control-plane), PR #19 (clock-skew handling).
- ClickHouse docs — cluster settings, materialized views, TTL, mutations.
- ClickHouse Cloud pricing & SLA pages (verify current).
