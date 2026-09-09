# Putting the demo corpus under a real Clerk org ("Nova")

**Recommendation: attach, via a supported admin command that does not exist yet. Do not hand-write
the UPDATE, and do not backfill a fresh tenant.** The reasoning, and the measurements behind it,
are below. Written 2026-09-09 against the live local stack.

## What the corpus actually is

Measured, not assumed:

| TenantId spelling | Spans | Range | Spans carrying an account hash | `daily_account_rollup` rows |
| --- | --- | --- | --- | --- |
| `38a6c264-9ca4-411c-92bb-b75433fe509c` (the UUID) | 512,081 | 2026-08-08 to 2026-09-08 | 470,303 | 6,711 |
| `local-dev` (the name) | 430,915 | 2026-08-04 to 2026-09-04 | 0 | 745 |
| `live-cto219` | 600,000 | 2026-08-12 to 2026-08-26 | 0 | 15 |

The UUID spelling is the only one that is a complete demo: it is the only one whose spans carry
account hashes, and it is the only one with a per-account rollup worth showing. That matters for the
choice, because per-customer attribution is the thing the product is differentiated on.

## Why attach, and not backfill

**Attach wins on the merits, and the margin is bigger than "no data movement".** The 6,711 rollup
rows under the UUID already cover the full month. `daily_account_rollup` is fed by a ClickHouse
materialized view that is an INSERT trigger on `otel_spans`, so it is forward-only: it captures what
arrives after it exists. Attaching keeps a rollup that is already built and already correct.

Backfilling a fresh tenant means re-inserting roughly 512,000 spans and rebuilding that rollup from
scratch. #348 verified that path at full corpus size, so it works, but it is a lot of moving parts
to re-run for a demo, and it doubles the storage for a corpus that is already right.

The usual argument for backfill is that it is the path a real customer takes. That argument is worth
less here than it looks: Nova is a demo of the product's numbers, not a rehearsal of onboarding, and
the onboarding path is exercised properly by the checks in `docs/clerk-provisioning-checklist.md`.

## Why attach needs a mechanism, and cannot be a hand-written UPDATE

The obvious form, `UPDATE tenants SET clerk_org_id = 'org_nova' WHERE id = '38a6c264-...'`, **fails**.
Verified against the live stack:

```
ERROR:  duplicate key value violates unique constraint "uq_tenants_clerk_org_id"
DETAIL:  Key (clerk_org_id)=(org_e2etest_nova_018) already exists.
```

Creating the Clerk org is what fires `organization.created`, and provisioning has no adopt path: it
sees no existing mapping and INSERTs a brand-new tenant with a new UUID, which **claims the org id**
before anyone can type the UPDATE. So the real manual procedure is not one statement, it is:

1. Create the Clerk org. A new empty tenant appears.
2. Find it, and `DELETE FROM tenants WHERE clerk_org_id = 'org_nova'`, which cascades across about
   thirty tables.
3. `UPDATE tenants SET clerk_org_id = 'org_nova' WHERE id = '38a6c264-...'`.

That is two destructive statements against production Postgres, in order, under time pressure,
where step 2 targets a row that is one typo away from being the corpus itself. It is a bad story,
and it is bad for exactly the reason the task suspected.

## The mechanism to build

**An admin command in the gateway, not an HTTP endpoint.**

`CLAUDE.md`'s "control-plane writes go through gateway endpoints" is about the request path: the web
app must never touch Postgres directly. Operator-run maintenance already has a different, established
shape in this repo, `gateway/seed.py`, which opens `psycopg.connect(settings.postgres_dsn)` and
writes `tenants` directly, run as `make seed` -> `docker compose exec gateway python -m gateway.seed`.
An adopt command belongs there, next to it.

It should **not** be an HTTP endpoint. An endpoint that moves `clerk_org_id` from one tenant to
another is a tenant-takeover primitive: whoever can call it can point their own Clerk org at any
customer's data. Putting that on the service-token surface, which the web app holds, widens the blast
radius of a leaked service token from "read the control plane" to "steal a tenant". A command that
requires shell access to the gateway task is the right privilege level for something run once.

Sketch, following the seed pattern:

- `python -m gateway.adopt_org --tenant <uuid> --clerk-org <org_id>`, one transaction.
- Refuse unless the target tenant currently has `clerk_org_id IS NULL`, so it can never move an org
  off a tenant that already has one.
- If another tenant already holds that org id, require it to be **empty** (no api_keys, no
  connector_configs, no value_events) and delete it in the same transaction; otherwise refuse and say
  which tenant holds it. That is what makes step 2 above safe instead of terrifying.
- Print the before and after rows.

This has not been built. It is the one thing standing between the current state and a Nova demo that
does not involve hand-written SQL.

## The spellings problem, and what it means for the demo

If Nova resolves to `38a6c264-...`, the dashboard binds `TenantId = '38a6c264-...'` into every
ClickHouse read, so the **430,915 spans written under the name `local-dev` stay invisible**, as do
the 600,000 under `live-cto219`. This is the `CLAUDE.md` trap: `/v1/batches` stores `TenantId` as
whatever spelling the caller posted, and the ingest path does not fold a name onto a UUID.

**For the demo, this is fine, and it should not be reconciled.** The UUID spelling is the corpus with
account attribution; the other two have none (0 spans carrying an account hash between them). Folding
them in would add a million spans of cost with no per-customer story attached, which would make the
"cost per account" screens look *worse*, not better, because the account-attributed share of total
spend would fall from most of it to about a third. The demo is stronger without them.

What should happen instead:

- Leave the rows alone. They are real telemetry from earlier work and deleting them destroys history.
- Know that any span count quoted from ClickHouse without a `TenantId` filter is roughly triple what
  the dashboard shows, which is a trap for anyone sanity-checking the demo.
- Treat this as evidence for fixing the ingest path rather than the data. A `/v1/batches` that
  resolved a posted name to the tenant UUID before storing, the way the control plane already does
  via `gateway.tenant_lookup.resolve_tenant_uuid`, would stop new instances of this. That is a real
  change with a migration question attached (what to do about the existing rows) and it is out of
  scope here, but it is the actual fix.
