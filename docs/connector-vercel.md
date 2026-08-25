# Vercel connector (CTO-163 compute, CTO-144 egress)

Vercel-hosted AI apps pay Vercel for both Functions compute and bandwidth. This connector pulls both
from the same usage payload and lands them next to the app's LLM spend on `/cost`. It is the only
connector that feeds two cost layers from one fetch, which is why the double-count rule below
exists.

- Compute: `infra/gateway/src/gateway/connectors/vercel.py` (`VercelCostConnector`,
  `VercelUsageClient`, `parse_vercel_compute`).
- Egress: `infra/gateway/src/gateway/connectors/egress.py` (`VercelBandwidthClient`,
  `parse_vercel_usage`).
- Control plane: `tenant_vercel_config` (`db/postgres/0016`) for compute,
  `tenant_egress_config` for egress.
- The HTTP dependency is imported lazily, so the gateway boots without it.

## Data source

One payload, `api.vercel.com` usage/billing, scoped by `team_id` and `project_id`. The response is
an `items[]` array where each entry carries a `date` (`YYYY-MM-DD`), an `amount` (decimal USD
string), and a `type`. The split is by line item type:

| Layer   | Line item types                              | Emits                                     |
| ------- | -------------------------------------------- | ----------------------------------------- |
| compute | `function_invocations`, `function_duration`   | one `compute` span/day, `GenAiSystem=vercel` |
| egress  | `bandwidth`                                  | one `egress` span/day, `GenAiSystem=vercel`  |

Amounts arrive as dollars, so no rate conversion is needed. Same-day items sum to one total per
layer. Each parser ignores the other layer's line items, so neither can absorb the other's spend.

## The double-count rule

CTO-144's egress connector already names Vercel bandwidth as an egress source. If this connector
also emitted egress, the Egress column would count Vercel twice. Two safeguards, in order:

The gate is primary. `VercelCostConnector` emits egress only when `emit_egress = true`
(`tenant_vercel_config.emit_egress`, default `false`). By default this connector owns the compute
half and Vercel egress flows solely through `tenant_egress_config`. Set the flag only for a tenant
that has no `tenant_egress_config` row for `egress_provider = 'vercel'`, so exactly one path emits.

Matching span ids are the backstop. When it does emit egress, it routes through CTO-144's
`EgressCostConnector` and `VercelBandwidthClient` verbatim, producing
`synthetic_span_id(tenant, 'vercel', 'egress', day)`. That is byte-identical to what CTO-144 would
produce, so even if both paths ran for the same day the base `span_exists` guard collapses them to
one row. A double-count is structurally impossible, not just unlikely.

## Auth

`access_token_ref` is a Secret Manager or KMS reference to the Vercel access token, never the raw
token, and the column is length-bounded to catch a pasted token. `team_id` and `project_id` are
public Vercel identifiers that scope the query, not secrets.

`enabled` (default `true`) lets a tenant keep the row but pause the connector.

## Emission and idempotency

Same as every cloud billing connector: one synthetic span per layer per day, cost set directly as
`gen_ai.cost.estimated_micro_usd`, `CostSource = estimated`, deterministic span id checked against
`span_exists` before insert. Re-running a day is safe.

## Failure behavior

A failed fetch stamps `failed` on the config row and emits no span. A tenant with no row is skipped.

## Tests

`infra/gateway/tests/test_connectors_vercel.py`, plus the Vercel egress cases in
`test_connectors_egress.py`. `parse_vercel_compute` and `parse_vercel_usage` are pure functions
tested against recorded fixtures; the clients are injected in tests and never hit the network.

## Open questions and gaps

**No backfill script.** `scripts/` has `backfill_compute.py`, `backfill_egress.py`, and
`backfill_gcp_compute.py`, but nothing for Vercel compute. A tenant enabling the Vercel connector
starts with an empty Compute column and no supported way to fill it. `VercelCostConnector` already
exposes `run_backfill`, so this is a thin script, not new plumbing.

**Nothing schedules it.** Same gap as the AWS connector. No daily worker calls `run()`.

**Config is now writable from the dashboard (CTO-176).** `/connectors` writes
`tenant_vercel_config` through `POST /v1/tenant/cost-connectors`, including the `emit_egress` flag.

**`emit_egress` is now checked across tables.** It used to be enforced by convention only: nothing
stopped an operator from setting `emit_egress = true` while a `tenant_egress_config` row for Vercel
also existed. `config_admin._upsert_vercel` now rejects that combination, so the config cannot claim
two owners for Vercel egress. The span id guard remains as defence in depth.
