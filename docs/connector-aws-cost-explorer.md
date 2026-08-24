# AWS Cost Explorer connector (CTO-143 compute, CTO-144 egress)

Pulls a tenant's daily AWS spend and lands it on `/cost` as synthetic spans, so the Compute and
Egress columns stop reading `$0`. AWS is the only provider that feeds both layers through the same
API (`ce:GetCostAndUsage`), with a different filter per layer.

- Compute: `infra/gateway/src/gateway/connectors/compute.py` (`AwsCostExplorerClient`,
  `parse_aws_cost_response`).
- Egress: `infra/gateway/src/gateway/connectors/egress.py` (`AwsEgressCostExplorerClient`), which
  reuses compute's response parser verbatim.
- Shared base: `connectors/base.py` (`CloudBillingConnector`) owns the emitter, the idempotency
  guard, the run contract, and the run recorder. Only the fetch differs per provider.
- Control plane: `tenant_compute_config` (`db/postgres/0011`, `0015`) and `tenant_egress_config`
  (`db/postgres/0012`).
- `boto3` is imported lazily inside the client. The gateway and the whole test suite import this
  module without it installed.

## Problem

Every live deployment shows `$0` in the Compute and Egress columns because no cloud billing data
ever reaches `otel_spans`. LLM spend is the only layer that arrives on its own, through the proxy.
Infrastructure spend has to be pulled.

## Data source

One call per run, `get_cost_and_usage` at `DAILY` granularity over `UnblendedCost`:

| Layer   | Filter                                                     | Config source                      |
| ------- | ---------------------------------------------------------- | ---------------------------------- |
| compute | Cost-allocation tags, default `{"tally:workload": "ai"}`    | `tenant_compute_config.tag_filter` |
| egress  | Usage type `DataTransfer-Out-Bytes`                        | fixed in code                      |

The tag filter is what keeps unrelated infrastructure out of the AI cost picture. A tenant that
doesn't tag its workload gets whatever the default matches, which is usually nothing, and that is
the honest answer rather than sweeping in the whole account.

Two details the parser handles that are easy to get wrong. Cost Explorer's `End` is exclusive, so
the client adds a day to include `end_day`. Days with zero or absent cost are dropped rather than
emitted as `$0` spans.

## Normalization

`parse_aws_cost_response` is a pure function over the raw response, unit-tested against recorded
fixtures. It reads `ResultsByTime[].TimePeriod.Start` and `ResultsByTime[].Total.UnblendedCost.Amount`
and returns `DailyCost(day, cost_micro_usd)`.

Money is integer micro-USD everywhere. Rate math uses `Decimal`, never float dollars.

## Emission

One synthetic span per day per layer, written through `gateway.mapping.span_to_row` with
`gen_ai.cost.estimated_micro_usd` set directly. There is no model to price, so the catalog
enrichment path is skipped. `GenAiSystem` lands as `aws`, `GenAiOperation` as `compute` or `egress`.

`CostSource` is `estimated`, which is correct: Cost Explorer numbers are un-invoiced. Reconciling
them against an authoritative invoice is the reconciler's job, not this connector's.

Idempotency matters because `otel_spans` is a plain `MergeTree` with no dedupe. Each span id is
derived deterministically from `(tenant_id, provider, operation, day)` via `synthetic_span_id`, and
the emitter checks `span_exists` before inserting. Re-running a day is safe.

## Auth

`credentials_ref` is a Secret Manager, KMS, or ARN pointer. Raw keys never appear in the database,
in this module, or in logs. The column is length-bounded so a fat-fingered raw key is more likely to
trip the check than land silently.

The literal value `aws-default-chain` means "use the ambient AWS credential chain" (instance role,
env, SSO). Any other value is treated as an assumable role ARN and passed through for the
deployment's STS wiring to resolve.

## Failure behavior

A failed fetch records a `failed` run on the config row and emits no span. The connector never
writes a guessed number. A tenant with no config row is a no-op, not an error.

## Operations

Backfill after enabling the connector, so a new tenant doesn't start with an empty column:

```bash
uv run python scripts/backfill_compute.py --tenant <uuid> --days 30
uv run python scripts/backfill_egress.py --tenant <uuid> --days 30
```

Both are idempotent on `(tenant_id, provider, day)`.

## Tests

`infra/gateway/tests/test_connectors_compute.py` and `test_connectors_egress.py`. Tests inject a
fake `BillingClient` and never touch the network.

## Open questions and gaps

Three things are missing before this is a product rather than a library.

**Nothing schedules it.** The connector classes and the backfill scripts exist, but no worker runs
them daily. Someone has to run the script by hand. The other ingest workers (Segment, HubSpot,
Pendo) share a cycle pattern in `gateway/integration_workers.py`, which is the obvious model to
follow, but the cloud billing connectors are not wired into it.

**Config is now writable from the dashboard (CTO-176).** `/connectors` has a Connect form per
source, backed by `GET/POST /v1/tenant/cost-connectors` and
`DELETE /v1/tenant/cost-connectors/{connector}` in `gateway.connectors.config_admin`. The form takes
a credential reference and rejects anything shaped like a raw key before it reaches Postgres.

One thing the schema forces and the UI now says out loud: `tenant_compute_config` is keyed on
`tenant_id` alone, so AWS and GCP compute are mutually exclusive per tenant. Connecting one replaces
the other, and the API returns which provider was replaced so the operator is not surprised.

**Cost Explorer costs money to query.** `GetCostAndUsage` is billed per request. A daily run per
tenant is cheap, but a backfill loop that calls per day rather than per range is not. The current
client fetches a range in one call, which is right. Any future scheduler should keep that shape.
