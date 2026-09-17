# Per-tenant price overrides (CTO-416)

What a tenant is charged for a model call comes from one of two places, and this page is the
authoritative statement of which one wins. It is written so a customer-facing answer can quote it.

## Precedence

For one span, cost is resolved per rate slot: `(provider, model, price_type)`, where `price_type` is
`input`, `output`, `cached_input`, `tool_call`, `vector_call` or `embedding`.

0. **Was the call billed per token at all?** A span the producer marks
   `gen_ai.cost.billing_mode = "subscription"` (CTO-417) carries no per-call price and is never
   priced from either table. It lands blank with `CostSource = 'subscription'`, which is a different
   statement from `'unpriced'`: there is no per-call rate to know, rather than one we are missing.
1. **The tenant's own override**, if one covers that slot on the span's date. This is the negotiated
   or committed-use rate from their contract.
2. **The public catalog** otherwise. These are the published list prices.

Within either pool, the exact model id is matched first and its family second (so
`claude-haiku-4-5-20251001` uses a rate listed for `claude-haiku-4-5` when no rate is listed for the
snapshot), and the most recent `valid_from` that is on or before the span's date wins.

**The date is the span's own**, not the date the span happened to be ingested. A backfilled or
late-arriving call is priced by the rate that was in force when the call was made, which is what
makes a corrected rate recomputable and what makes an old invoice explainable. A span older than
every applicable window is unpriced rather than priced at today's rate.

**A rate can be scheduled ahead.** Appending an entry with a future `valid_from` does not disturb
the rate in force today: both windows are live, and each span resolves to the one covering its date.

Precedence is per slot, not per model. A tenant with a negotiated input rate and no negotiated
output rate is billed contract input and list output, which is what most contracts actually say.

The rule is implemented once, in `PriceCatalog.lookup` (`sdk/python/src/tally/pricing.py`), and
every cost figure in the product goes through it.

## The ledger

Overrides live in `price_catalog_overrides` (Postgres, migrations 0001 and 0035) and are read
through `tally.overrides.OverrideLedger`. The table is an **append-only, versioned, audited ledger**:

- Every entry carries a `version` for its slot, the `actor` who made the change, the `reason`, the
  `recorded_at` timestamp, and the `supersedes` version it replaces.
- Re-pricing a slot **appends a new version**. Nothing is updated in place, so the rate a span was
  priced at in March is still readable in June and a past invoice stays explainable. A database
  trigger refuses `UPDATE` outright.
- Withdrawing a rate **appends a tombstone** (an entry with no price) rather than deleting a row.
  The withdrawal itself stays in the trail.

A tombstone **closes** the override rather than erasing it, and its `valid_from` is the date the
override ENDS:

- A revocation filed in advance ("this contract ends on 1 January") keeps pricing at the contract
  rate until that date, and falls back to the public catalog from it.
- A backdated one ("it ended on 1 August", filed in September) ends the override on 1 August.
- Either way, a span from **before** the end date is still priced at the rate that was in force when
  the call was made, so a backfill, a late arrival or a reconciliation rerun over a pre-revocation
  date reports what the customer was actually charged.
- A revocation says there is no override from its date onward, so a window that was scheduled
  earlier for a later date does not survive it. To set a new rate after a revocation, append it
  after the revocation: the ledger is read in order and the last statement about a date wins.

## Changing a price

Through the gateway control plane. The dashboard never writes Postgres directly.

```
GET  /v1/tenant/price-overrides                  # active rates (+ ?history=true for the audit trail)
POST /v1/tenant/price-overrides                  # append a rate, or {"revoke": true} for a tombstone
POST /v1/tenant/price-overrides/refresh          # re-read the ledger on this replica now
```

On a revocation (`{"revoke": true}`), `valid_from` is the date the override ends and defaults to
today; `valid_to` is refused, because a tombstone closes a window rather than opening one.

A rate is a decimal **string** (`"2.40"`), never a float: money that passes through a float has lost
precision before it reaches the column. `actor` and `reason` are required. The replica that takes
the write applies it immediately; other replicas pick it up within
`TALLY_PRICE_OVERRIDES_REFRESH_TTL_S` (60 seconds by default). The response says which of the two
happened in `applied`, and `applied` is false when nothing has loaded the entry, including when the
feature is switched off.

The `unit` must be one the cost math can apply to that tier: per-million-tokens for `input`,
`output`, `cached_input` and `embedding`, per-call for `tool_call` and `vector_call`. The GET
response lists the allowed units per tier. Rates are USD only: nothing in the cost path converts a
currency, so a non-USD rate would be reported as USD spend and is refused.

Loading the ledger is enabled with `TALLY_PRICE_OVERRIDES_ENABLED` (on in `infra/docker-compose.yml`,
off in the code default so a checkout without Postgres boots unchanged).

## When the ledger cannot be read

Cost lands **blank**, not at list price. `EstimatedCost` is `NULL` and `CostSource` is `'unpriced'`
for every tenant-scoped span until a load succeeds, `/readyz` reports the gateway as `degraded` for
`price_overrides`, and the gateway logs the failure at ERROR.

Readiness itself is **not** affected: the replica stays in rotation and keeps accepting telemetry.
Pricing metadata that cannot be read is a reason to stop asserting a cost, never a reason to stop
accepting a customer's spans.

This is deliberate. A contract rate is normally below list, so pricing from the public catalog while
the contract is unreadable would report spend the customer never incurred, in a number that looks
exactly like a real one. A blank is recoverable and visible; a confident wrong figure is neither.
The cost is that spans go unpriced for every tenant during such an outage, including tenants who
hold no override, because telling those two groups apart requires reading the table that is down.
It clears itself on the next successful refresh, with no restart and no backfill: the spans written
during the outage keep their honest blank.
