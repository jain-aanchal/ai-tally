# Cloudflare connector (CTO-144)

Pulls a tenant's daily bandwidth-out from Cloudflare and lands it as synthetic `egress` spans.
Cloudflare is the one egress source that reports bytes instead of dollars, so it is the only
connector that needs a tenant-supplied price to produce a cost at all. That single difference drives
most of the design below.

- Code: `infra/gateway/src/gateway/connectors/egress.py` (`CloudflareAnalyticsClient`,
  `parse_cloudflare_bytes`, `_bytes_to_micro`).
- Control plane: `tenant_egress_config` (`db/postgres/0012`) with `egress_provider = 'cloudflare'`.
- Shared base: `connectors/base.py` (`CloudBillingConnector`), reused verbatim. Only the fetch
  differs.
- The HTTP client is imported lazily.

## Data source

Cloudflare's GraphQL analytics API at `https://api.cloudflare.com/client/v4/graphql`, using the
`httpRequests1dGroups` aggregate scoped to the configured zone:

```
data.viewer.zones[].httpRequests1dGroups[] -> { dimensions { date }, sum { bytes } }
```

`resource_id` on the config row is the Cloudflare zone id. It is a public identifier, not a secret.

Multiple zones or groups on the same day sum, so a tenant running several zones still gets one
egress span per day rather than one per zone.

## Pricing bytes

This is the part that separates Cloudflare from Vercel and AWS. The analytics API returns bytes, not
money. `usd_per_gb` on the config row converts them, and `_bytes_to_micro` does the arithmetic in
`Decimal`.

`usd_per_gb` is `NULL` for Vercel and AWS, which report USD directly, and required for Cloudflare. A
Cloudflare row without it fails soft: the run records `failed` and emits no span. We will not invent
a price for bytes. An operator has to state the rate their Cloudflare plan actually charges, because
we cannot derive it from the analytics response.

Days with zero bytes are dropped rather than emitted as `$0`.

## Emission and idempotency

One synthetic span per day, `GenAiSystem = cloudflare`, `GenAiOperation = egress`, cost set directly
as `gen_ai.cost.estimated_micro_usd`, `CostSource = estimated`.

Span ids come from `synthetic_span_id(tenant, 'cloudflare', 'egress', day)`, checked against
`span_exists` before insert, so re-runs never double-count.

A tenant can run Cloudflare alongside Vercel and AWS egress at once. Each provider is its own
control-plane row and its own run, keyed on a distinct provider, so the three sum cleanly on the
Egress column with each counted once.

## Auth

`credentials_ref` is a Secret Manager, KMS, or ARN pointer to the Cloudflare API token. Never a raw
token. The column is length-bounded to catch a pasted credential.

## Failure behavior

Two ways this connector declines to produce a number, both recording `failed` and emitting no span:
the fetch itself fails, or `usd_per_gb` is missing. A tenant with no config row is skipped.

## Operations

```bash
uv run python scripts/backfill_egress.py --tenant <uuid> --days 30
```

The script covers all three egress providers, Cloudflare included, and is idempotent on
`(tenant_id, provider, day)`.

## Tests

`infra/gateway/tests/test_connectors_egress.py`. `parse_cloudflare_bytes` is a pure function tested
against a recorded fixture. The GraphQL runner is injected in tests.

## Open questions and gaps

**The GraphQL query is a placeholder.** `_cloudflare_query` is marked `pragma: no cover` and builds
the query by string interpolation of the zone id and dates. It has never run against live
Cloudflare. Two things to settle before it does: whether `httpRequests1dGroups` is the right
aggregate for billable bandwidth (Cloudflare bills on more than HTTP request bytes for some plans),
and whether the zone id should be parameterized rather than interpolated. The interpolation is not
an injection risk today because the zone id comes from our own config row and not user input, but it
is the kind of thing that stops being true later.

**`usd_per_gb` is a flat rate.** Cloudflare pricing tiers by plan and by region. A single rate per
tenant will be wrong at the margins for anyone on a metered plan. Whether that matters depends on
how large the Egress column is relative to the rest of the bill, and on this demo data it is small.

**Config is now writable from the dashboard (CTO-176).** `/connectors` writes the Cloudflare row
through `POST /v1/tenant/cost-connectors`. The form requires a zone id and a `usd_per_gb` rate, and
the gateway refuses the row without them rather than letting a priceless Cloudflare connector sit
there failing every run.

**Nothing schedules it.** Same gap as the AWS and Vercel connectors. See
`docs/connector-aws-cost-explorer.md`.
