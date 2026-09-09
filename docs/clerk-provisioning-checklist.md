# Clerk provisioning: what it does, and what to set before you deploy

**Status: exercised end to end against the local stack on 2026-09-09, at `74c308c`.** Every status
code below was observed, not read off the source. The one thing that could not be checked here is
`SecretManagerKeyProvider` against real AWS; see "Not verified" at the bottom.

This is the companion to `docs/onboarding-flow-audit.md`. The audit says what the path does and what
is missing. This says what to set so the path works, and what each variable does when it is wrong.

## The chain, with the statuses it actually returns

```
Clerk organization.created
  -> POST /api/webhooks/clerk       web, svix-verified   200 | 400 | 401 | 500 | 502
  -> POST /v1/tenant/provision      gateway, svc-token   200 | 401 | 422 | 503
  -> tenant row + HMAC key ref      tenant_provisioning.py
  -> GET /v1/tenant/by-clerk-org/ID gateway, svc-token   200 | 401 | 404
  -> getTenant() resolves           web/lib/getTenant.ts
  -> Home renders
```

Observed on a clean run: `by-clerk-org` 404 before the event, webhook 200, `by-clerk-org` 200
returning a fresh tenant UUID with `plan: free`, one `tenants` row carrying its own
`hash_salt_kek_ref`, and one `usage_limits` row. A brand-new tenant has **zero ingest API keys**;
that is still the audit's finding #6, not something provisioning does.

## The environment, both sides

Set these together. The pairs are pairs: half of one is worse than neither.

### Gateway

| Variable | Set it to | What a wrong value does |
| --- | --- | --- |
| `TALLY_REQUIRE_API_KEY` | `true` | Left `false` in production, the control plane is **unauthenticated**: anyone who can reach the gateway can provision tenants, read `by-clerk-org`, and mint ingest keys. The local stack runs it `false` on purpose, so this is the single most important line to change. |
| `TALLY_GATEWAY_SERVICE_TOKEN` | a shared secret (`openssl rand -hex 32`) | Empty while `TALLY_REQUIRE_API_KEY=true` and the gateway **refuses to boot**: `RuntimeError: ... the control-plane service-token gate cannot be enforced. Refusing to start.` (`app.py:298`). That is deliberate and correct. Observed. |
| `TALLY_HMAC_KEY_PROVIDER` | `kms` | Left at the default `local`, per-tenant HMAC material is derived from a root secret sitting in config, which is what the credentials-by-reference invariant forbids on a multi-tenant instance. It will appear to work, which is the danger. `kms` also needs the gateway's `[secrets]` extra installed (boto3) and Secrets Manager grants on the task role; without boto3 the provider raises a `ProvisionError` naming the missing extra. |
| `TALLY_POSTGRES_DSN` | the control-plane DSN | Provision raises and the webhook route returns 502, so Clerk retries forever and no org ever gets a workspace. |
| `TALLY_SCHEDULER_ENABLED` | `true`, if you want attribution | Off by default. Feature-level attribution, cost connectors and reconciliation never run. Not a provisioning failure, but a new tenant's dashboard stays thin without it. |

### Web

| Variable | Set it to | What a wrong value does |
| --- | --- | --- |
| `CLERK_WEBHOOK_SIGNING_SECRET` | the svix secret from the Clerk dashboard's webhook endpoint | Unset, the route returns **500** with `webhook signing secret not configured`, so Clerk retries and nothing provisions. Wrong value, every delivery is **401 `invalid signature`** and no tenant is ever created, silently, because Clerk's retries all fail the same way. Observed both. |
| `GATEWAY_SERVICE_TOKEN` | **exactly** the gateway's `TALLY_GATEWAY_SERVICE_TOKEN` | A mismatch is the most likely production misconfiguration and it is quiet: the webhook forwards, the gateway answers **401**, the route turns that into a **502**, and Clerk retries forever. On the dashboard side every control-plane read 401s too, so a signed-in customer gets the error boundary ("This page could not be loaded") on Home. Observed. |
| `TALLY_GATEWAY_URL` | the private gateway URL | Unreachable, the route returns **502 `provision unreachable`** within 10s and Clerk retries (CTO-359; before that fix it was an unhandled `TypeError: fetch failed` and a bare 500). |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | the Clerk instance's keys | Without them the product path cannot authenticate anyone. |
| `TALLY_DEV_TENANT` | **unset** | Set, the dashboard pins one tenant and Clerk is never consulted, so every visitor sees that tenant's data. A production build refuses to start on this alone (CTO-268); turning it on for real additionally needs `TALLY_ALLOW_INSECURE_NO_AUTH=1`. Do not set either on anything holding real data. |

In the Clerk dashboard, the webhook endpoint must point at
`https://<dashboard-host>/api/webhooks/clerk` and subscribe to `organization.created`. That route is
public in `web/middleware.ts` by design and is authenticated solely by its svix signature.

## Failure modes, and what each one does

All observed against the live stack.

| Scenario | Result |
| --- | --- |
| Valid signature, new org | 200. One tenant, one key reference, one `usage_limits` row. |
| **Redelivery** (Clerk retries) | 200, `created: false`. Same tenant id, **same** `hash_salt_kek_ref`: the fast path mints nothing, so a retry cannot roll a tenant's HMAC key. |
| **Concurrent first delivery** | Eight threads against real Postgres settle on one tenant: one `created: true`, eight key references minted, seven deleted by the loser-cleanup, one kept. No orphaned key material, no second tenant. |
| **Bad signature / wrong secret** | 401, and `fetch` is never called, so nothing reaches the gateway. |
| **Missing svix headers** | 400. |
| **Non-`organization.created` event** | 200 ack, no provision. |
| **Key provider unavailable** | Gateway 503 `tenant key provider unavailable; provisioning was not performed`, webhook 502, and **zero** tenant rows written. A tenant that cannot hash is never created. |
| **Gateway unreachable** | Webhook 502 `provision unreachable` (CTO-359). |
| **The race** (browser beats the webhook) | `by-clerk-org` 404, and Home renders "Setting up your workspace" and polls until the workspace appears (#358). Before #358 this was a 500 crash page. |

One thing worth knowing about the race: a permanently failing provision produces the **same** 404
from `by-clerk-org` as the transient race, because in both cases no row exists. `#358` distinguishes
the 404 from other statuses, so a 401 or 503 gets the failure screen, but "provisioning has been
failing for ten minutes" still presents as "setting up". The 45-second bound in
`WorkspaceProvisioning` is what stops that being forever.

## Not verified here

- **`SecretManagerKeyProvider` against real AWS.** It has unit tests against an injected fake and it
  was exercised here only through a deliberately failing provider. Whether `create_secret` plus
  `update_secret_version_stage` succeed under a real task role, and whether the resulting ARN fits
  the `no_raw_secret` CHECK in practice, has still never been observed. Do this first in a scratch
  account.
- **Real Clerk.** No Clerk account was used. Deliveries were signed with the real svix scheme and a
  local secret, and the Clerk session was stubbed, so `auth()` returning an active org is the one
  hop taken on trust. Clerk's actual delivery latency, which is what sets the width of the race
  window, is still unmeasured.
- **Clerk's retry behaviour end to end.** The route returns the retriable codes; that Clerk then
  retries on the schedule its docs describe was not watched.
