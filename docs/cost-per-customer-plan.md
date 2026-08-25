# Build plan: Cost per customer

Implementation plan for the feature scoped in `docs/cost-per-customer-scope.md`. Common components
first, then the account dimension, then the tab.

Each numbered item below is intended to be one Linear ticket.

## Decisions taken

**Decision 1. Optional account label, falling back to the hash.**
A tenant may attach a human-readable label to an account. Where one exists the tab shows it; where
one does not, it shows a shortened hash. Labels are opt-in per account, so a tenant that wants no
customer names in our system simply sets none and the tab still works.

Where the label lives matters, and it should not be a span column. A label is mutable metadata that
can be renamed, and stamping it on every span both wastes storage and creates the question of which
of two labels wins. It also puts customer names in the telemetry store, which is the thing the hash
exists to avoid.

So: labels live in a Postgres control-plane table (B7), keyed on the account hash, joined at render
time. Telemetry stays name-free. The label can be set through the API or picked up opportunistically
from an optional `account_label` SDK attribute that the gateway upserts into the table and does not
write to the span.

B6 stays in scope: it is how an operator finds an account that has no label.

**Decision 2. Direct cost only for v1. No allocation.**
Workstream C moves out of v1 and becomes a follow-up. The tab reports directly attributable spend
(LLM, tools, vector, embeddings) and excludes compute and egress.

Consequence to design around: on the current demo tenant that excludes about 47 percent of spend, so
every account figure understates true cost by roughly half. This must be stated on the page, and
stated with the tenant's real number rather than a vague disclaimer. We already know the excluded
total, so D3 becomes a banner that reads "excludes $44,500 (47%) of compute and egress that cannot
yet be attributed per account", computed live per tenant.

---

## Workstream A: shared components

Build first. Justified independently of this feature: 10 pages currently hand-roll `<table>` markup
and the honest-blank `—` is hand-written across 10 files. Every later tab in the backlog needs the
same primitives.

**A1. `DataTable` component.**
A typed table taking a column spec (key, header, align, render) and rows. Handles right-aligned
numerics with `tabular-nums`, the shared header style, empty state, and horizontal overflow.
Includes column sorting and pagination from the start: account lists are unbounded, and retrofitting
paging into a component three pages already use is more expensive than building it once.

**A2. Honest-value primitives.**
`<Money>`, `<Pct>`, and `<Blank reason>`. `Blank` renders the `—` with a hover explaining *why* it is
blank ("no revenue events wired", "below 50 samples"). Today that reason is either missing or a
hand-written `title` attribute. This is the product's central posture and it should be one component,
not 21 copies.

**A3. Migrate two existing pages onto A1 and A2.**
`/connectors` and `/attribution`. Proves the abstraction against real usage before the new tab depends
on it, and pays down duplication. Deliberately not migrating all 10: if the component does not fit two
very different pages, it is the wrong component.

---

## Workstream B: the account dimension

The foundation. Nothing in the tab works without it, and it ships dark until a customer instruments
it.

**B1. ClickHouse migration: `AccountIdHash`.**
Add `AccountIdHash FixedString(64)` and `AccountIdHashKeyVersion` to `otel_spans`, and
`AccountIdHash` to `business_events`. Additive, defaults to `''`, which the UI reads as unattributed
rather than as a customer named "unknown". Next free migration number is 0022.

**B2. SDK: `account_id`.**
Accept `account_id` alongside `user_id`, hash it with the existing per-tenant HMAC path in
`hmac_keys.py`, and emit `gen_ai.account_id_hash` plus the key version. Same treatment as user ids:
never stored raw, not joinable across tenants.

**B3. Gateway: map the attribute.**
`mapping.py` writes the new columns. The Go edge proxy passes an account header through for tenants
who use the proxy rather than the SDK.

**B4. `daily_account_rollup` table and materialized view.**
Mirrors `daily_feature_rollup`: keyed on `(TenantId, AccountIdHash, Day)` with `FeatureTag` and
`GenAiOperation`, carrying cost, span count, and a `uniq` state for distinct users. Keeps the tab fast
when an account count runs into the thousands.

**B5. `account_id` in the identity graph.**
Add to the `IdentityAType` and `IdentityBType` enums so a CRM or CDP connector can stitch users to
accounts. This is the path for tenants who cannot tag every span. Stitched accounts must be
distinguishable from tagged ones in the UI.

**B6. Account lookup endpoint.**
`POST /v1/tenant/account-lookup` taking a plaintext account id and returning its hash, computed with
the tenant's own HMAC key. Per Decision 1 this is how an operator locates an account that carries no label.
The plaintext is used to compute the hash and is never persisted or logged. Feeds a search box on
the tab.

**B7. Account label store.**
Postgres table `tenant_account_labels (tenant_id, account_id_hash, label, updated_at)` with CRUD
through the gateway, plus an optional `account_label` SDK attribute the gateway upserts here rather
than writing to the span. Labels are optional per account. ClickHouse never holds a customer name.
Deleting a label reverts that account to its hash, which is the escape hatch for a tenant who
changes their mind.

---

## Workstream C: allocation (deferred, not in v1)

Out of scope per Decision 2. Kept here because the tab is incomplete without it and it should be the first
follow-up. The reconciliation test in C1 is the reason this cannot be bolted on carelessly later.

**C1. Allocation engine, pure functions.**
Given per-account direct spend and a tenant-level shared total, return each account's allocated
share. Implements the rule chosen in D2, plus even-split as an alternative. Pure and unit-tested with
no I/O, matching the house style of `unitEconomics.ts` and `projection.py`.

The test that matters: allocated shares must sum to the shared total, and direct plus allocated must
sum to the tenant total. If per-account costs do not reconcile with `/cost`, the tab loses trust
immediately.

**C2. Per-tenant allocation config.**
Which rule is in force, stored on a tenant config row and surfaced on the page. Small, and it stops
the rule from being a hardcoded assumption nobody can see.

---

## Workstream D: the tab

**D1. Queries.**
`queryAccountCosts` (per account: direct by layer, distinct users, span count) and
`queryAccountDetail` (one account: layer split, top features, 30-day trend). Both follow the existing
tenant-scoped helpers in `clickhouse.ts`. Include an explicit unattributed bucket.

**D2. The page.**
Route `/cost-per-customer`, added to `NAV` in `Shell.tsx`. Headline carries total attributed spend,
account count, and **the share of spend with no account**, which is the honesty valve: if 60 percent
is unattributed, the page says so at the top rather than ranking the other 40 percent as if it were
the whole picture. Table built on A1 and A2. The Account column renders the label from B7 when one
exists and a shortened hash otherwise, with the full hash available on hover and copy.

**D3. Excluded-cost banner.**
Per Decision 2 the tab shows direct cost only, so the page must say what it leaves out, using the tenant's
real figure rather than a generic caveat. Queries the compute and egress total for the window and
renders "excludes $X (N%) of compute and egress that cannot yet be attributed per account". Replaced
by the allocated columns when workstream C lands.

**D4. Account detail view.**
Layer split, top features, cost trend, heaviest agent runs. Mostly existing query shapes with one
extra `WHERE AccountIdHash = ...`.

**D5. Onboarding empty state.**
Every existing tenant sees an empty tab on day one because nobody emits an account id yet. The empty
state does two jobs, in this order:

1. **Explain what this page is for.** Someone landing here has never seen it. Say plainly that it
   breaks AI spend down by the tenant's own customers, so they can see which accounts are expensive
   and which are worth their price, and what it will look like once data arrives.
2. **Say how to turn it on.** The `account_id` SDK snippet, the optional label, and a note that data
   appears as soon as the next spans land.

Not a blank table, and not a bare "no data" line.

---

## Workstream E: revenue and margin

Where the tab stops being a cost report. Two findings from scoping reshaped this stream.

**Stripe revenue is already account-shaped.** `StripeConnector` sets `user_id` to the Stripe
**customer** id, and a Stripe Customer is the account in B2B SaaS. So account-level revenue is
already arriving; it is being hashed into `UserIdHash` as though it were an end user. HubSpot does
the same with a deal or company object id. Routing those into `AccountIdHash` is most of the work,
and it is far less than building a new revenue pipeline.

**That mismatch is probably why margin is blank**, alongside the source filter. Spans hash an end
user; Stripe events hash a customer. The two only join if a tenant happens to use one identifier for
both.

**E1. Revenue source configuration.**
Replace the hardcoded `Source = 'stripe'` filter in `queryAttribution`. `business_events.ValueType`
already distinguishes `monetary`, `mrr`, `refund` and `count`, which is the right discriminator, and
`Source` is an unconstrained string. Config names which sources count as revenue for a tenant,
defaulting to any monetary source from a configured revenue connector. Fixes the underlying design
rather than loosening one filter.

**E2. Route Stripe and HubSpot identity to `AccountIdHash`.**
The Stripe customer id and the HubSpot company or deal id become the account, not the user. Covers
subscription revenue and closed-won contract revenue with no new ingestion path.

**E3. Revenue per account query.**
Sum `ValueAmountMicro` by `AccountIdHash`, netting refunds, over the same window as cost.

**E4. Margin column and profitability ranking.**
Revenue minus total cost per account. Renders a blank when revenue is unknown, never 0.

**E5. CSV revenue upload.**
For tenants whose revenue lives in a finance system with no usable API, or in a spreadsheet. Upload
`account_id, period, amount, currency`, mapped to the same `business_events` shape as every other
source so nothing downstream special-cases it.

Two things this must get right. It is a **point-in-time snapshot**: revenue changes monthly and an
upload that is never refreshed goes stale silently, so the page shows an "as of" date and a
staleness badge in the same way the reconciliation freshness badge already works. And re-uploading
the same period must replace rather than append, or revenue doubles.

**E6. Generic revenue API.**
A documented endpoint accepting `{account_id, amount, currency, occurred_at, event_name}` for
Chargebee, Recurly, Zuora, or a home-grown biller. `WebhookIngestor` already exists in the SDK, so
this is largely a matter of exposing and documenting a stable contract.

Deliberately not in scope: a data-warehouse connector (Snowflake, BigQuery) that queries revenue
where it is already modelled. That is the highest-fidelity source for a company of any size and the
right long-term answer, but it is its own project and should not gate this tab.

## Sequencing

```
A1, A2 ──► A3
              └──► D1, D2, D3, D4, D5
B1 ──► B2 ──► B3 ──► B4 ──┘
   ├──► B5 (parallel, optional for v1)
   ├──► B6 (v1: find an unlabelled account)
   └──► B7 (v1: optional labels)
E1 ──► E2 ──► E3 ──► E4 (after D2 ships)
   E5, E6 parallel, either can precede E3
C1 ──► C2 (follow-up, replaces D3)
```

A1, A2 and B1 can start now and are independent of each other. B2 through B4 are a chain. B6 and B7
need only B1. D needs A and B. E needs E1 resolved before anything else in that stream, and should
not gate the tab shipping. C is deferred.

**Minimum shippable:** A1, A2, B1 to B4, B6, B7, D1, D2, D3, D5. A working tab showing direct cost
per account, labelled where the tenant chose to label and searchable where they did not, stating
both the unattributed share and the excluded infra cost.

## Constraints decided

**One user belongs to one account. Multi-account users are not supported.**
This closes open question 4 in the scope and removes the reconciliation hazard that came with it:
nothing splits or duplicates a user's cost across accounts, so per-account totals sum cleanly to the
tenant total.

Cost attribution is unaffected either way, because each span carries its own `AccountIdHash` at emit
time. The assumption only bites on the stitching path (B5) and on revenue (E2), where an account is
inferred from a user rather than stated. There, if a user is observed against more than one account,
the pipeline does not guess: it attributes nothing for that user and raises it as a data-quality
finding, so the situation is visible rather than silently mis-attributed.

## Risks carried from the scope

- Ships dark: empty for every existing tenant until someone instruments `account_id`.
- Reconciliation: per-account totals must sum to the tenant total or the tab contradicts `/cost`.
- A user belonging to several accounts is unresolved (open question 4 in the scope). Duplicating cost
  across accounts inflates the total and breaks the reconciliation test in C1.
- Accounts with no label still show as a hash, so a tenant who sets no labels gets a table they can
  search (B6) but not scan. Acceptable because labelling is entirely in their control.
- Direct-cost-only (Decision 2) means every figure understates true cost by roughly half on current data. The
  D3 banner is the mitigation and it is not optional.
