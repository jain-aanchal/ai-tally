# Initiative 1: Organizations, users & access (real identity)

Status: build-ready spec, and largely shipped. Owner: platform. Linear epic: see the
"Organizations, users & access" epic in team CTO Assist.

This is the roadmap-facing spec for Initiative 1. It defines the problem, the target
end state, the data model, the API surface, the auth and middleware design, the
migration plan, how `local-dev` is retired, the security posture, and the open
questions. A deeper design-level companion already exists at
`docs/specs/initiative-1-orgs-and-access.md`; this document is the roadmap view and
adds an honest as-built status map (section 13), because most of the initiative has
already landed on `main`.

## 1. Problem

Today ai-tally is single-tenant. Before this initiative the dashboard hardcoded one
tenant (`process.env.TALLY_TENANT_ID ?? "local-dev"`, repeated across roughly a dozen
`web/lib` and `web/app` files), the gateway control-plane endpoints trusted an
unauthenticated `x-tenant-id` header, and there was no login. Anyone who could reach
the gateway could read or write any tenant's config by naming it. There was no signup,
no organizations, no roles, and no way for a team to hold its own API keys.

The initiative replaces that with real organizations and login: a stranger can sign
up, get an organization provisioned as an ai-tally tenant, invite teammates with
roles, hold real per-org ingest API keys, switch between orgs they belong to, and see
only their own org's data. The org IS the tenant, end to end, with its own data, its
own HMAC secret (a Secret Manager reference), and its own API keys it creates and
rotates.

## 2. Target end state

- Signup and login for the dashboard through Clerk. Clerk owns users, organizations,
  memberships, roles, and invitations.
- Every new Clerk organization is provisioned as an ai-tally `tenants` row with its own
  UUID, its own per-org HMAC key set (stored only as a Secret Manager or KMS reference),
  and its own ingest API keys.
- The dashboard reads the active `orgId` and `orgRole` from the Clerk session, resolves
  the org to a tenant UUID, and scopes every read and every control-plane write to it.
  No hardcoded name on the product path.
- Per-org ingest keys are minted, listed (metadata only), rotated, and revoked from the
  dashboard, and kept in ai-tally's own control plane so the Go edge proxy verifies them
  in the hot path with no Clerk round-trip.
- `local-dev` disappears from the product path and survives only behind an explicit
  local-development escape hatch so `make up`, the demo, and CI need no Clerk account.

## 3. Data model

Clerk is the system of record for identity. ai-tally does NOT create Postgres `users`
or `memberships` tables. It stores only:

- The org-to-tenant mapping: `tenants.clerk_org_id` (nullable, partial-unique so at most
  one tenant maps to a given Clerk org, and any number of tenants may have NULL, which in
  practice is only the `local-dev` demo tenant).
- The per-org HMAC key reference: `tenants.hash_salt_kek_ref`, which already existed and
  carries `CONSTRAINT no_raw_secret CHECK (hash_salt_kek_ref NOT LIKE 'sk-%' AND
  length(hash_salt_kek_ref) < 512)`. It holds a Secret Manager or KMS reference, never
  raw key material.
- The ingest keys: `api_keys`, which already keys on `tenant_id UUID REFERENCES
  tenants(id) ON DELETE CASCADE`, stores `key_hash TEXT NOT NULL UNIQUE` (SHA-256 of the
  token), and a `scope` CHECK of `read | write | admin`. Migration `0029` added the
  display metadata columns `name`, `token_prefix`, `created_by` (Clerk user id, audit
  only), and `last_used_at`.

The groundwork migration `db/postgres/0029_orgs_and_access.sql` is merged. It adds
`clerk_org_id` to `tenants` and the four display columns to `api_keys`, all additive and
nullable so the pre-existing `local-dev` tenant and its key survive untouched. No new org
or membership tables are needed, by the Clerk-is-source-of-record decision, so no further
migration is required for the core model. Any future follow-up (for example an
append-only audit table for key actions) takes the next free migration number after the
current highest (`0030`) and adds the matching `infra/docker-compose.yml` init mount.

## 4. API surface

All control-plane writes go through the gateway. The web app never touches Postgres
directly; it reads ClickHouse for telemetry and calls the gateway for control-plane
reads and writes. Every gateway store method resolves the caller's tenant onto
`tenants.id` via `gateway.tenant_lookup.resolve_tenant_uuid` before touching a
tenant-scoped row, so SQL never crosses tenants.

`resolve_tenant_uuid` accepts three identifier forms: a `tenants.id` UUID (fast path,
parsed first), a Clerk org id (`org_...`, matched before the name), and the tenant name
(`local-dev`, the dev and dashboard spelling, matched last). The Clerk-org and name
lookups are two ordered single-column queries, deliberately not one combined `OR`, so a
name that happens to look like another tenant's org id cannot resolve nondeterministically
to the wrong row.

Gateway endpoints (all under the `GATEWAY_SERVICE_TOKEN` control-plane gate, section 6):

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/tenant/provision` | Provision a tenant from a verified `organization.created` event. Idempotent and race-safe. Returns `{ tenant_id, plan, created }`. |
| GET | `/v1/tenant/by-clerk-org/{org_id}` | Resolve a Clerk org id to `{ tenant_id, plan }`, or 404. The web app calls this once per session and caches it. |
| GET | `/v1/tenant/keys` | List key metadata for the tenant. Never returns a secret. |
| POST | `/v1/tenant/keys` | Mint a new ingest key. Returns the raw token exactly once. |
| POST | `/v1/tenant/keys/{id}/rotate` | Mint a replacement and revoke the old row in one transaction. Returns the new token once. |
| DELETE | `/v1/tenant/keys/{id}` | Revoke a key (`revoked_at = now()`). A real revoke, not a delete. Double-revoke is not an error. |
| GET | `/v1/tenant/hmac-key` | The SDK's in-process HMAC bootstrap (Initiative 2). Returns the tenant's active key material and version, gated on the caller's own ingest key. |
| GET | `/v1/edge/keys?since={cursor}` | The edge proxy's key-cache delta feed. Returns `{ changes: [{ key_hash, tenant_id, scope, revoked_at }], cursor }`. |

The raw ingest token is `tally_sk_live_` plus a high-entropy CSPRNG suffix, shown exactly
once at mint or rotate and never stored. Only its SHA-256 (`key_hash`, hashed with the
same `auth.py::hash_key` ingest uses) is persisted, so a minted key authenticates
byte-for-byte on the `/v1/batches` hot path and in the edge proxy. `token_prefix` is a
non-secret leading slice kept only so a human can tell two keys apart in a list.

## 5. Auth and middleware design (web)

- `@clerk/nextjs` is a dependency; `svix` verifies the provisioning webhook.
- The root layout (`web/app/layout.tsx`) wraps the product path in `<ClerkProvider>`. In
  dev-escape-hatch mode it renders a bare body, because mounting Clerk with no keys would
  throw.
- `web/middleware.ts` runs `clerkMiddleware()`, protects every route, and treats
  sign-in, sign-up, and `POST /api/webhooks/clerk` as public (the webhook carries no
  Clerk session and is authenticated by its svix signature inside the route). When the
  session has a user but no active org, it redirects to `/select-org`. When the dev
  escape hatch is set, the middleware export is a no-op pass-through.
- `web/lib/getTenant.ts` is the single tenant seam. `getTenant()` returns
  `{ tenantId, orgId, orgRole }`. On the product path it reads `{ orgId, orgRole }` from
  Clerk `auth()`, throws when there is no active org, and resolves `orgId` to a tenant
  UUID via `GET /v1/tenant/by-clerk-org/{orgId}` with a short in-process cache. In dev
  mode it short-circuits to the pinned dev tenant and never imports Clerk.
  `controlPlaneHeaders(tenantId)` attaches `x-tenant-id` plus the service-token bearer;
  `canManage(tenant)` gates admin-only actions on `orgRole === 'org:admin'`.
- The app chrome (`web/components/Shell.tsx`) shows Clerk's `<OrganizationSwitcher>` and
  `<UserButton>` on the product path, and a static local-dev badge under the escape hatch.
  `web/app/settings/keys` is the key-management UI (list, create-once-token modal, rotate,
  revoke), and `web/app/settings/members` embeds Clerk's `<OrganizationProfile>` for
  invites, role changes, and removals. ai-tally builds no membership UI of its own.

The ClickHouse read filter binds the tenant UUID as a query parameter
(`WHERE TenantId = {tenant:String}`). Tenant resolution runs outside the mock-fallback
try block in `web/lib/clickhouse.ts`, so a missing org or a gateway outage surfaces as an
honest error rather than silently rendering another tenant's demo data.

## 6. Control-plane auth (closing the gap)

The web server is the only legitimate caller of the control-plane endpoints. It
authenticates to the gateway with a server-only shared secret, `GATEWAY_SERVICE_TOKEN`,
sent as `Authorization: Bearer <token>`. The gateway rejects control-plane requests that
lack a valid service token with 401. The gate is active only when auth is on
(`require_api_key`) and a token is configured, so local dev with auth off is unaffected;
starting with `require_api_key` on but no token configured fails fast.

The service token authenticates the web SERVER to the gateway. It does not identify the
tenant or the human. The web server still passes the resolved tenant UUID in
`x-tenant-id`, and is trusted to have checked the Clerk session and org membership first.
Authorization (which human may do what) is enforced in the web server against the Clerk
`orgRole` before any control-plane write: minting, rotating, and revoking keys, and
managing members, require `org:admin`; a member gets a read-only dashboard. This split,
service token authenticates the transport and Clerk role authorizes the action, keeps the
gateway hot-path-free and makes the web server the single policy enforcement point.

## 7. Provisioning flow

A new Clerk org becomes a tenant through a webhook. Clerk emits `organization.created`,
svix-signed, to `web/app/api/webhooks/clerk/route.ts` (the gateway is private in the
hosted topology, so Clerk cannot reach it directly). The web route verifies the svix
signature, rejects on failure, and on a verified event forwards to the gateway
`POST /v1/tenant/provision` with the service token, returning the gateway result so
Clerk's own retry and backoff apply on a 5xx.

Inside the gateway provisioner:

1. Fast path: an existing `clerk_org_id` mapping means a redelivery. Return the existing
   tenant and mint no new key material.
2. On a miss, mint the per-org HMAC key set BEFORE inserting, and store only its
   reference in `hash_salt_kek_ref`. A mint failure raises and no tenant row is written,
   so a tenant that cannot hash is never created.
3. Insert race-safely with `INSERT ... ON CONFLICT (clerk_org_id) WHERE clerk_org_id IS
   NOT NULL DO NOTHING RETURNING id`, repeating the partial-index predicate so Postgres
   infers `uq_tenants_clerk_org_id`. The winner also seeds `usage_limits` on the free
   plan.
4. The loser of a race adopts the winning tenant and deletes the key set it just minted,
   so no orphaned key material survives.

## 8. Canonical TenantId

The canonical `TenantId` on a span is the tenant UUID. Authenticated ingest already tags
spans with the key's tenant UUID and refuses a body claiming a different tenant. The edge
proxy resolves the presented `X-Tenant-Key` (via the `/v1/edge/keys` delta feed, hashing
the token with the same SHA-256 the gateway uses) to a tenant UUID and stamps that UUID on
its telemetry. The seed prints the tenant UUID, and the demo backfill is invoked with that
UUID (the `Makefile` derives `TENANT_UUID` from Postgres and passes `--tenant`), so demo
data is written under the UUID and a UUID-scoped dashboard read sees it.

## 9. Migration plan

- `0029_orgs_and_access.sql` is merged, with its compose mount. It is additive and every
  statement is `IF NOT EXISTS`, so re-applying is safe. On a running stack it must be
  applied by hand (`make psql < ../db/postgres/0029_orgs_and_access.sql`), because
  `docker-entrypoint-initdb.d` runs only on a first boot against an empty volume.
- No new org or membership tables are required for the core model. A future audit table
  or `organization.deleted` bookkeeping (see open questions) would take the next free
  number after `0030` and add the matching compose mount.

## 10. Retiring local-dev

`make up`, the demo, and CI must still work with no Clerk account. The escape hatch:

- `TALLY_DEV_TENANT` (a tenant UUID). When set, `getTenant()` short-circuits to that
  tenant and never touches Clerk, and the middleware export is a no-op, so app routes are
  reachable with no session.
- The gateway service-token gate is off when `require_api_key` is off, matching today's
  local behavior.
- The `TALLY_TENANT_ID ?? "local-dev"` default is gone from the product code paths. The
  only surviving `local-dev` literal is behind the explicit dev flag (and in test config).
  A production build with the flag unset and no active Clerk org refuses to serve tenant
  data rather than falling back to `local-dev`.

## 11. Security

- Keys are hashed and shown once. Only `key_hash` (SHA-256) is stored; the raw token is
  returned exactly once and never again; `token_prefix` cannot authenticate.
- The per-org HMAC key set is stored only as a Secret Manager or KMS reference in
  `hash_salt_kek_ref`, honoring the length-bounded `no_raw_secret` CHECK. Each org gets a
  distinct key set at provision, so user and account hashes cannot be joined across
  tenants. Raw key material never touches Postgres or a log; a mint failure fails the
  provision.
- The production HMAC provider is a KMS or Secret Manager seam selected by config;
  selecting it without wiring a client fails fast rather than silently dropping to
  dev-derived material.
- Role checks are enforced in the web server against the Clerk `orgRole` before any
  control-plane write. The gateway sees only the service token and the resolved tenant.
- Honest under uncertainty: `last_used_at` is null until a best-effort off-hot-path job
  stamps it, never guessed; a missing org resolution is a 404, never a silent `local-dev`.

## 12. Risks and open questions

Items that must not be guessed. They are recorded here rather than answered.

1. Clerk custom roles and paid tier. A true `owner` role and finer-grained non-admin roles
   need Clerk custom roles, a paid-tier feature. P1 ships on the free `admin` / `member`
   split. Whether and when to upgrade is a product and cost decision.
2. `organization.deleted` handling. When a Clerk org is deleted, what happens to the
   tenant and its data (soft-disable and retain for a grace period, or hard-delete)? The
   webhook route handles only `organization.created` today. This needs a decision before
   GA and must not be guessed.
3. Data residency and `region`. `tenants.region` exists and defaults. Whether an org
   chooses a region at creation, and whether Clerk org metadata carries it, is out of P1
   scope.
4. Plan and billing source. New orgs land on `plan = 'free'`. Where a plan change comes
   from (a billing-provider webhook, a manual admin action, Clerk org metadata) is
   undefined.
5. Existing `local-dev` demo data. Reconciling shared demo rows tagged with the old name
   once canonical `TenantId` is the UUID. Recommended path is re-seed against a fresh
   volume; a shared demo environment may prefer an in-place ClickHouse mutation.
6. Signup access mode. Open self-serve vs invite-only for the beta. Recommendation:
   invite-only allowlist for the beta, open later. This is a Clerk setting, not ai-tally
   code, and the choice is the owner's.
7. `last_used_at` stamping job. The column exists and the UI renders it honestly (null
   until known). The best-effort off-hot-path job that stamps it is not built; its
   mechanism (sampled ingest, periodic aggregate) is a design choice, not to be guessed
   into the request path.
8. Edge-proxy hardening. Key lookup is an in-memory map-equality on the SHA-256 hash, not
   a constant-time compare, and the proxy does not validate the `tally_sk_live_` prefix.
   The package rationale is that hashes are non-reversible and in-memory only. Whether to
   add constant-time semantics is a security-review call.

## 13. As-built status

Most of this initiative has already landed on `main`. This map is honest about what
exists so the epic and its tickets reflect reality rather than re-planning shipped work.

P1, Clerk auth and orgs (shipped):
- `@clerk/nextjs` and `svix` dependencies, `<ClerkProvider>`, `web/middleware.ts` with
  `clerkMiddleware()` and the no-active-org redirect, `<OrganizationSwitcher>` in the
  shell, sign-in / sign-up / select-org pages, and `web/lib/getTenant.ts` as the tenant
  seam. `TALLY_DEV_TENANT` escape hatch works; `local-dev` is off the product path.

P2, org-to-tenant mapping (shipped):
- Migration `0029` merged. `web/app/api/webhooks/clerk/route.ts` verifies svix and
  forwards `organization.created`. `gateway.tenant_provisioning` provisions idempotently
  and race-safely with a per-org HMAC reference honoring the CHECK.
  `GET /v1/tenant/by-clerk-org/{org_id}` resolves org to UUID.
  `resolve_tenant_uuid` matches `clerk_org_id`. The `GATEWAY_SERVICE_TOKEN` gate is in
  place. Canonical `TenantId = UUID` across ingest, edge-proxy telemetry, seed, and demo
  backfill.

P3, keys and secrets (shipped):
- `gateway.tenant_api_keys` implements mint, list (metadata only), rotate, and revoke,
  wired at `GET/POST /v1/tenant/keys`, `POST /v1/tenant/keys/{id}/rotate`, and
  `DELETE /v1/tenant/keys/{id}`. The dashboard key UI (`web/app/settings/keys`) and member
  UI (`web/app/settings/members`) exist. The edge proxy consumes the `/v1/edge/keys` delta
  feed and verifies keys in the hot path with no Clerk round-trip.
  `GET /v1/tenant/hmac-key` serves the SDK's in-process HMAC bootstrap (Initiative 2).

Not built (tracked as open questions above): `organization.deleted` handling, the
`last_used_at` stamping job, plan and billing source, data-residency routing, Clerk custom
roles, and the edge-proxy constant-time-compare hardening.

Documentation drift found while writing this: the `CLAUDE.md` Conventions line stating the
dashboard passes the tenant NAME is stale now that the dashboard resolves the Clerk org to
a UUID. Corrected alongside this spec.
