# Scope: "Cost per customer" tab

**Status: proposal, not built.** This is a scoping doc for a new dashboard tab that answers "what
does each of our customers cost us in AI spend, and are they profitable?"

The short version: the reporting and UI are the easy half. The hard half is that ai-tally has no
concept of a customer today, and roughly half of a tenant's bill cannot be attributed to one
without an allocation rule we have to choose deliberately.

## The ask

A B2B AI product charges per seat or per account. Its AI costs do not scale the same way: one
enterprise account running a research agent all day can cost more than a thousand self-serve
accounts combined. Today a tenant can see cost per feature (`/cost`), cost per user (Home's ROI
snapshot) and cost per conversion (`/attribution`), but not cost per account. So they cannot answer:

- Which customers are unprofitable at their current price?
- Which customers should be on an enterprise plan?
- What is gross margin per account, and how has it moved since we shipped the agent?
- Is our biggest logo also our biggest cost centre?

That last question is the one that sells the tab.

## Why this does not exist today

There is no account dimension anywhere in the pipeline. This is the central finding of the scope.

`otel_spans` carries `TenantId` (the ai-tally customer) and `UserIdHash` (an individual end user),
with no column between them. `business_events` and `attribution_records` are the same: keyed on
`UserIdHash`. `identity_graph` types are `user_id`, `anonymous_id`, `session_id`, `email` and
`external_id`, none of which is an account.

"Cost per user" already works because `UserIdHash` is on every span. Cost per customer needs a
grouping that nothing currently emits. Everything below follows from closing that one gap.

## Decision 1: what is a customer

Three readings, and they produce different products.

**An account or organisation** (recommended). The tenant's own paying customer: a company, a
workspace, a team. This is what a B2B SaaS means by "customer" and what makes the margin question
answerable, because revenue arrives per account (a Stripe subscription, a contract).

**An individual end user.** Already available as cost per user, and adding a tab for it would
duplicate the ROI snapshot. Not worth a tab.

**A billing subscription.** Precise for margin but wrong for product questions, since one account
can hold several subscriptions and churn between them.

Recommendation: model the account, and let a tenant map subscriptions onto accounts later. Concretely
this means one new identity, `AccountIdHash`, that a tenant sets on its telemetry the same way it
sets `user_id` today.

## Decision 2: names, and the PII invariant

This is the decision most likely to be got wrong quietly, so it needs settling before code.

User ids are never stored raw. They are HMAC-SHA256'd under a per-tenant key
(`sdk/python/src/tally/hmac_keys.py`) so a hash cannot be reversed or joined across tenants. Account
ids carry the same risk and should be hashed the same way.

But a "Cost per customer" table where every row reads `9f86d081884c7d65...` is useless. The operator
needs to see "Acme Corp" to act on it. Three options:

1. **Hash only, tenant resolves names client-side.** Safest, and the least usable. The tenant would
   have to paste a lookup table into the browser for the page to mean anything.
2. **Hash for storage, optional display label supplied at query time.** The tenant's own system holds
   the mapping and passes labels through the dashboard session. We never persist the name.
3. **Store an account display name alongside the hash.** Most usable, and it puts customer names in
   our database, which is a change to the product's privacy posture and needs an explicit decision
   plus a DPA review.

Recommendation: build on option 2, and treat option 3 as a separate opt-in with its own ticket.
Account names are business-confidential rather than personal data in most cases, so option 3 is
defensible, but it should be a deliberate choice and not a side effect of shipping a tab.

## Decision 3: allocating shared cost, the hard part

Direct LLM, tool, vector and embedding spend can be attributed to an account, because those spans
carry the account once it is set. Compute and egress cannot. They arrive from cloud bills through
the connectors (see `docs/connector-aws-cost-explorer.md`) as one synthetic span per provider per
day at tenant level, with no per-request detail to attribute.

On the current demo tenant, compute and egress are about 47 percent of the bill. So a cost-per-customer
number that silently ignores them would understate the true cost of every account by roughly half,
which is exactly the failure the product exists to fix. Reporting only the directly attributable half
would be the single worst outcome of this feature.

Four allocation rules, in increasing order of defensibility:

| Rule | How | Honest? |
| --- | --- | --- |
| Ignore shared cost | Report direct spend only | No. Understates every account, contradicts the hidden-cost thesis |
| Even split | Shared cost / account count | Weakly. Punishes small accounts, flatters heavy ones |
| Pro rata on direct spend | Share shared cost in proportion to each account's direct LLM spend | Reasonable default. Assumes infra scales with model usage |
| Pro rata on a driver | Share on requests, tokens, or GB served per account | Best, needs the driver metered per account |

Recommendation: pro rata on direct spend for v1, with the rule named on screen and the allocated
portion shown as a separate column rather than folded silently into one total. An allocated number
presented as a measured one is the kind of quiet dishonesty the rest of this product avoids. The UI
should let the reader see direct, allocated, and total side by side.

## Data model

One new identity column, mirroring `UserIdHash` in type and treatment:

```
otel_spans.AccountIdHash          FixedString(64)   -- HMAC-SHA256, per-tenant key, '' when unset
otel_spans.AccountIdHashKeyVersion LowCardinality(String)
business_events.AccountIdHash     FixedString(64)
```

Additive and backward compatible: existing rows get `''`, which the UI reads as unattributed rather
than as a customer named "unknown". A tenant that never sets an account sees an empty tab with an
onboarding prompt, not a broken one.

A rollup mirroring `daily_feature_rollup` keeps the page fast at high account counts:

```
daily_account_rollup (TenantId, Day, AccountIdHash, FeatureTag, GenAiOperation)
  EstimatedCost, ReconciledCost, SpanCount, UserCountState AggregateFunction(uniq, FixedString(64))
ORDER BY (TenantId, AccountIdHash, Day)
```

Add `account_id` to the `identity_graph` type enum so CDP and CRM connectors can stitch an account
to its users, which is what makes the tab work for a tenant that cannot easily tag every span.

## Ingestion

**SDK.** `account_id` alongside `user_id`, hashed with the same per-tenant HMAC path, emitted as
`gen_ai.account_id_hash`. This is the primary path and the one to build first.

**Gateway.** `mapping.py` maps the attribute onto the new column. The proxy passes it through from a
header for tenants that use the edge proxy rather than the SDK.

**Stitching, for tenants that cannot tag spans.** Derive the account from the user via
`identity_graph` when a CRM or CDP connector supplies the user-to-account edge. Lower confidence, and
the UI should say which accounts were stitched rather than tagged.

## The page

Route `/cost-per-customer`, added to `NAV` in `web/components/Shell.tsx`.

Headline: total attributed spend, count of accounts with spend, and the count unattributed. That
last number is the honesty valve. If 60 percent of spend has no account, the page says so at the top
instead of ranking the 40 percent as if it were the whole picture.

Main table, one row per account, sorted by total cost:

| Column | Notes |
| --- | --- |
| Account | Display label when available, else a short hash |
| Users | Distinct `UserIdHash` seen for the account |
| Direct cost | LLM, tools, vector, embeddings. Measured |
| Allocated cost | Compute and egress share. Clearly labelled as allocated |
| Total cost | Direct plus allocated |
| Revenue | From `business_events` when the account maps to monetary events, else `—` |
| Gross margin | Revenue minus total cost. `—` when revenue is unknown |
| Cost per user | Total cost / distinct users |

Detail view per account: cost split by the six layers, top features, cost trend over 30 days, and
the heaviest agent runs for that account. Most of this reuses existing query shapes with one extra
`WHERE AccountIdHash = ...`.

Honest-under-uncertainty applies throughout, as elsewhere in the product. Margin renders `—` when
revenue is not wired, never 0. Accounts below a minimum span count render a low-sample marker rather
than a noisy cost-per-user figure.

## Phasing

**Phase 1, make it possible.** The `AccountIdHash` column, SDK and gateway support, migrations, the
rollup. No UI. A tenant can tag spans and query by account. This is the phase that carries all the
risk and most of the value.

**Phase 2, the tab.** The table, direct cost only, with an explicit banner saying shared infra cost
is not yet included. Ships something usable while the allocation debate is settled.

**Phase 3, allocation.** Pro rata shared cost, direct and allocated shown as separate columns, the
rule named on screen and configurable per tenant.

**Phase 4, margin.** Join revenue per account, gross margin, and a profitability ranking. This is
where the tab stops being a cost report and becomes the thing people buy.

Phases 1 and 2 are the minimum that ships anything. Phase 3 is what makes the number trustworthy.

## Open questions

1. Do we store account display names, or resolve them at query time? Decision 2. Blocks the UI
   design and needs a privacy call, not an engineering one.
2. Which allocation rule ships as the default, and is it tenant-configurable? Decision 3.
3. Should an account with no revenue mapping show `—` for margin, or should the tab require Stripe to
   be connected before it appears in the nav at all?
4. How do we handle an end user who belongs to several accounts (a consultant, a shared support
   inbox)? Split the cost, duplicate it, or attribute to first-seen? Duplicating inflates the total,
   which breaks reconciliation against the tenant's bill.
5. What is the expected account cardinality? A tenant with 5 accounts and one with 500,000 need
   different table designs and different pages.

## Risks

**The tab is empty for every existing tenant on day one.** Nobody is emitting an account id, so this
ships dark and only lights up after a customer instruments it. The onboarding prompt matters more
than usual here, and Phase 1 should land well before the tab is announced.

**Reconciliation.** Per-account costs must sum to the tenant total, or the tab will contradict
`/cost` and lose trust immediately. Duplicated attribution (open question 4) and rounding across
many small accounts are both live ways to break that. This deserves a test that asserts the sum.

**Scope creep into a billing product.** Cost per customer plus revenue per customer is one step from
customer-level invoicing and margin forecasting. Worth deciding early where this tab stops.
