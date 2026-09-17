# Per-tenant price overrides (CTO-416)

What a tenant is charged for a model call comes from one of two places, and this page is the
authoritative statement of which one wins. It is written so a customer-facing answer can quote it.

## Precedence

For one span, cost is resolved per rate slot: `(provider, model, price_type)`, where `price_type` is
`input`, `output`, `cached_input`, `tool_call`, `vector_call` or `embedding`.

1. **The tenant's own override**, if an active one covers that slot on the span's date. This is the
   negotiated or committed-use rate from their contract.
2. **The public catalog** otherwise. These are the published list prices.

Within either pool, the exact model id is matched first and its family second (so
`claude-haiku-4-5-20251001` uses a rate listed for `claude-haiku-4-5` when no rate is listed for the
snapshot), and the most recent applicable `valid_from` wins.

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
  The slot then falls back to the public catalog, and the withdrawal itself remains in the trail.

## Changing a price

Through the gateway control plane. The dashboard never writes Postgres directly.

```
GET  /v1/tenant/price-overrides                  # active rates (+ ?history=true for the audit trail)
POST /v1/tenant/price-overrides                  # append a rate, or {"revoke": true} for a tombstone
POST /v1/tenant/price-overrides/refresh          # re-read the ledger on this replica now
```

A rate is a decimal **string** (`"2.40"`), never a float: money that passes through a float has lost
precision before it reaches the column. `actor` and `reason` are required. The replica that takes
the write applies it immediately; other replicas pick it up within
`TALLY_PRICE_OVERRIDES_REFRESH_TTL_S` (60 seconds by default).

Loading the ledger is enabled with `TALLY_PRICE_OVERRIDES_ENABLED` (on in `infra/docker-compose.yml`,
off in the code default so a checkout without Postgres boots unchanged).

## When the ledger cannot be read

Cost lands **blank**, not at list price. `EstimatedCost` is `NULL` and `CostSource` is `'unpriced'`
for every tenant-scoped span until a load succeeds, `/readyz` reports `price_overrides: false`, and
the gateway logs the failure at ERROR.

This is deliberate. A contract rate is normally below list, so pricing from the public catalog while
the contract is unreadable would report spend the customer never incurred, in a number that looks
exactly like a real one. A blank is recoverable and visible; a confident wrong figure is neither.
The cost is that spans go unpriced for every tenant during such an outage, including tenants who
hold no override, because telling those two groups apart requires reading the table that is down.
It clears itself on the next successful refresh, with no restart and no backfill: the spans written
during the outage keep their honest blank.
