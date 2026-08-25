# Scope: hosted ai-tally

**Status: proposal, not built.** A managed multi-tenant service, so evaluating ai-tally does not
require standing up ClickHouse, Postgres, Redpanda and MinIO first.

## One correction to the premise

It is not only docker-compose on a laptop. `deploy/` already carries real self-host artifacts: Helm
charts for GKE and EKS, ECS task definitions and service definitions with IAM policies, Cloud Run
service YAML, and a Vercel path. Someone who wants to self-host on AWS or GCP has a supported route
today.

What is missing is a version **we** run, where a prospect signs up and sends their first span in ten
minutes. That is a different product with different problems, and the gap is not packaging. It is
the two things below.

## The two real blockers

Everything else in this doc is ordinary infrastructure work. These two are the feature.

### 1. The dashboard has no authentication at all

There is no login page, no session handling, no middleware, no `next-auth`, nothing. Anyone who can
reach the URL sees every number. That is defensible for a tool running on your own laptop or behind
your own VPN. It is disqualifying for a hosted service.

This is not a small addition. It means identity, sessions, an invite flow, password or SSO, roles,
and an audit trail for who changed a connector or a guardrail.

### 2. The tenant is a deploy-time environment variable

Every data function in the dashboard resolves the tenant the same way:

```ts
const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";
```

That appears in `clickhouse.ts`, `tenant.ts`, `cac.ts`, `costConnectors.ts` and elsewhere. One
dashboard deployment serves exactly one tenant, permanently, and the tenant is baked in at deploy
time.

Hosted multi-tenancy requires the tenant to come from the authenticated session on every request
instead. That is a change to the signature of essentially every query function in `web/lib`, and it
has to be done in a way where forgetting it is impossible rather than merely discouraged.

The gateway is in better shape. It already resolves tenants per request from a bearer key or header
(`_resolve_tenant_for_control_plane`), stamps `TenantId` on every row, and holds per-tenant HMAC key
versions for user hashing. The backend was built multi-tenant. The dashboard was not.

## Isolation, and the thing that will keep you up at night

Tenant isolation today is **application-level filtering**. Every ClickHouse query carries
`WHERE TenantId = {tenant:String}`. There is no row-level security, no per-tenant database, and no
enforcement below the query text. One missing `WHERE` clause in one query leaks one customer's spend
to another.

On a single-tenant self-hosted deployment that risk is theoretical, because there is only ever one
tenant's data present. Hosted makes it a live, unbounded risk.

Three ways to harden it, in increasing cost:

| Approach | Isolation | Cost |
| --- | --- | --- |
| Keep app-level filtering, add a lint or test that every query is tenant-scoped | Weakest, one bug from a leak | Low |
| Route all reads through a single tenant-scoped query helper that cannot be bypassed | Strong if the seam holds | Medium |
| Database or cluster per tenant | Strongest, and it kills noisy neighbors too | High, and it complicates migrations |

Recommendation: the query helper seam, plus a test that fails when a raw query string containing a
table name appears outside it. Make the safe path the only path. Consider a separate ClickHouse
cluster for large enterprise tenants later, sold as a feature rather than built for everyone.

Worth noting what is already right: user ids are HMAC'd under a **per-tenant** key, so a hash cannot
be joined across tenants even if data did mix. That decision pays off here.

## Metering and billing

`gateway/metering.py` already exists, which is a head start. Hosted needs a billing model, and the
choice shapes the architecture:

- **Per span or event ingested.** Predictable for us, scary for a customer whose traffic spikes.
- **Per tracked spend.** A percentage of the AI spend observed. Aligns incentives and is easy to
  explain, but it prices the customer's growth rather than our cost.
- **Seats or flat tiers.** Simplest to sell, weakest link to our actual cost, which is dominated by
  ClickHouse storage and query volume.

Whatever is chosen, storage retention becomes a pricing lever and therefore a product decision. The
tiering work in `db/clickhouse/storage_tiering.sql` is the mechanism.

## What else hosted needs that self-host does not

**Onboarding without a terminal.** Sign up, create a tenant, mint an API key, see a curl or an SDK
snippet, watch the first span land. The `/onboarding` page is a start; it assumes the tenant already
exists.

**Operations.** On-call, alerting on our own infrastructure, backup and restore that someone has
actually rehearsed, a status page, and an upgrade path that does not involve a customer running
migrations by hand. Today migrations are mounted into a container's init directory, which only runs
on first boot. That was already a real bug for self-host (two connector migrations never applied).
Hosted needs a proper migration runner.

**Compliance.** SOC 2 is the usual price of entry for the buyers this product targets, since it sits
next to their spend data. That is a months-long programme, not a sprint, and it should start early
because it constrains logging, access, and retention decisions.

**Data residency.** Some buyers will require EU-only storage. Deciding this late means re-architecting
storage; deciding it early means one more axis in the deployment model.

**Limits.** Per-tenant rate limits already exist in the gateway. Hosted also needs query-side limits,
because one tenant running expensive dashboard queries degrades everyone.

## Phasing

1. **Dashboard auth and session-derived tenancy.** Nothing else can ship first. This is the bulk of
   the work and the highest risk.
2. **The isolation seam** plus the test that enforces it. Do this alongside phase 1, not after.
3. **Self-serve onboarding**: signup, tenant creation, API key issuance, first-span confirmation.
4. **Operations**: migration runner, backups, monitoring, status page.
5. **Metering and billing.**
6. **Compliance and residency**, started in parallel because of lead time.

Phases 1 and 2 are the feature. Phases 3 through 6 are what makes it a business.

## Open questions

1. Single-tenant-per-deployment hosted (we run an isolated stack per customer) or true multi-tenant?
   The first is far easier to secure and far more expensive to run. It is a viable first step for
   enterprise deals and a bad long-term answer for self-serve.
2. Does hosted stay feature-identical with self-host, or do they diverge? Divergence is where open
   core products usually get into trouble.
3. What is the free tier, and what stops it from being a way to store unlimited telemetry for free?
4. Who is on call, and what is the SLA? A cost observability tool being down is annoying; losing a
   customer's telemetry is not recoverable.
5. Is the tenant model one tenant per company, or per team? The current schema assumes a tenant is
   the unit of isolation and billing, and teams within a company will want separation without
   separate billing.

## Risks

**Underestimating the auth and tenancy work.** "Add login" sounds like a sprint. Replacing a
process-level tenant constant with a session-derived one across every query function, safely, is not.

**A leak ends the company.** This product holds customers' AI spend, which is competitively
sensitive. Application-level filtering with no defence in depth is the current posture, and it needs
upgrading before the first external tenant, not after.

**Running ClickHouse as a service is its own job.** It is not a database you operate casually at
multi-tenant scale. Managed ClickHouse Cloud is worth pricing against the engineering cost.

**Self-host stops being a first-class citizen.** Once hosted exists it gets the attention, and the
self-host path quietly rots. Worth deciding up front whether self-host remains supported and how
that is tested.
