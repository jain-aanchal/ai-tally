# Runbook: creating and populating tenants

Task-oriented. For what provisioning does internally see
[clerk-provisioning-checklist.md](clerk-provisioning-checklist.md); for the reasoning behind putting
the demo corpus under a real org see [nova-demo-tenant.md](nova-demo-tenant.md).

A tenant is a row in Postgres `tenants` plus everything keyed on its UUID in ClickHouse. The UUID is
canonical. The name is an accepted spelling on gateway control-plane calls only, and the ingest path
does NOT fold a name onto a UUID, so spans posted under a name are invisible to a UUID-bound
dashboard read. That is not a theoretical hazard: this stack carries 430,915 spans under `local-dev`
and 512,081 under the matching UUID, which are the same logical tenant split by a spelling mistake at
ingest.

## Create a tenant

### The product way: create a Clerk organization

What a real customer does, and the only path exercised in production. Sign in, open the organization
switcher at the foot of the sidebar, and create one. Clerk emits `organization.created`, the webhook
verifies its svix signature and forwards it, and the gateway provisions the tenant and mints its HMAC
key.

**This does not complete on localhost.** Clerk cannot reach `localhost`, so the webhook never
arrives, and the page sits on "Setting up your workspace" until it gives up after 45 seconds. That is
the honest failure rather than a bug. To exercise real delivery locally, put a tunnel (ngrok,
Cloudflare Tunnel) in front of port 3000 and register that public URL as the Clerk webhook endpoint.

### Provision directly

The webhook's own payload, so it is the same code path minus delivery. Use this when you want a
tenant genuinely bound to a Clerk org without setting up a tunnel.

```bash
curl -s -X POST http://localhost:8080/v1/tenant/provision \
  -H 'content-type: application/json' \
  -d '{"clerk_org_id":"org_XXXXXXXX","name":"Acme Corp"}'
```

Returns `{"tenant_id":"...","plan":"free","created":true}`. It is idempotent and race-safe: a repeat
with the same org id returns the existing tenant and mints nothing, and concurrent first deliveries
resolve to one tenant with the losers cleaned up. It accepts a raw Clerk event
(`{"data":{"id","name"}}`) as well as the flattened fields.

In production this endpoint requires the control-plane service token. It is only open here because
the local gateway runs with `require_api_key` off.

### `make seed`

```bash
cd infra && make seed
```

A tenant plus an API key and feature tags, with no Clerk involvement. It prints the UUID to put in
`TALLY_DEV_TENANT`. Useful for testing with auth bypassed; not a path any customer takes.

## Find an organization id

From the Clerk dashboard, or from the gateway log after creating the org, where it is whatever is
returning 404:

```bash
docker logs --tail 40 ai-tally-gateway-1 2>&1 | grep -oE 'by-clerk-org/[A-Za-z0-9_]+ HTTP/1.1" [0-9]+' | tail -3
```

## Give a tenant data

```bash
npx tsx examples/vercel-chatbot/scripts/backfill-spans.ts --tenant <TENANT_UUID> --seed 138
```

Thirty days of backdated spans: no LLM calls, no API keys, no spend. It produces the accounts,
features, conversions and waste findings the demo relies on. Pass the **UUID**, never the name, for
the reason at the top of this file.

This path is verified end to end at full corpus size: 512,056 spans in 1,229 batches with zero shed,
reproducing the documented `$52,401.67` monthly LLM figure. It has a run-level circuit breaker that
aborts after two consecutive shed batches, so a gateway that is down fails in minutes rather than
grinding for days.

## Point an organization at an existing tenant

Only safe **before** anything has claimed that org id:

```bash
docker exec ai-tally-postgres-1 psql -U tally -d tally -c \
  "UPDATE tenants SET clerk_org_id='org_XXXXXXXX' WHERE id='<TENANT_UUID>' AND clerk_org_id IS NULL"
```

The `AND clerk_org_id IS NULL` guard is not decoration. `uq_tenants_clerk_org_id` is a partial unique
index on non-null values, so without the guard you can silently move an org between tenants.

**This does not work in production.** There, creating the org fires the webhook within seconds and a
fresh tenant claims the org id first, so the same statement fails on that index. Recovering by hand
means a cascading DELETE across roughly thirty tables followed by the UPDATE, in order, against
production. That is what `adopt_org` exists to prevent, and it is specced but not built. Do not
improvise it mid-cutover.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Setting up your workspace", then times out | `by-clerk-org` returns 404: no tenant for the active org | Provision it, or attach an existing tenant |
| Signed in but bounced to `/select-org` | No active organization. The product has no personal workspace | Create or pick an org |
| `API /api/home failed: 404` | The server-to-self fetch is not carrying the session cookie | Fixed in #362. If it recurs, check `web/lib/api.ts` still forwards `cookie` |
| Dashboard renders but every number is a dash | ClickHouse unreachable. `tryLive()` catches and returns null | Check `TALLY_CLICKHOUSE_URL` and that the host is reachable from where the app runs |
| A brand new tenant shows populated ROI figures | Mock fallback on an empty result | Being fixed. Until then treat any data on a fresh tenant as fabricated |
| Dashboard empty despite ingested rows | Spans tagged with the tenant NAME, dashboard reads the UUID | Re-send with `tenant_id` set to the UUID |

## Production differences

Development and production Clerk instances share no users and no organizations, so an org id from
one does not exist in the other. Creating "Nova" in production yields a different id than the
development one, and any attach done locally does not carry over.

Production also needs its own Google OAuth credentials. Development instances borrow Clerk's shared
ones, which is why social sign-in works locally with no setup and will not in production.

Before deploying, run `make prod-preflight`. It checks the environment on both sides, including that
the two service tokens match byte for byte and that `TALLY_DEV_TENANT` is unset, and it names the
symptom each failure produces. A green run is a floor, not a guarantee: it cannot check Fargate
ARM64 availability in your region, your RDS engine version, whether the Clerk webhook URL points at
this deployment, or whether Postgres has been migrated.
