# Scope: self-hosting ai-tally and linking it from your website

**Status: proposal.** One organization (ours) runs one ai-tally instance on its own cloud account,
for its own AI spend, and links to it from its own marketing site.

## What this is and is not

This is the single-operator deployment. One tenant, our data, our AWS account, our domain. It is not
`docs/hosted-version-scope.md`, which scopes a managed multi-tenant service where strangers sign up
and we operate their telemetry. That doc's hard problems (tenant isolation below the query layer,
self-serve onboarding, metering and billing, SOC 2, on call for other people's data) are all out of
scope here, because there is exactly one tenant and we are it. What the two share is the deployment
substrate, so work done here is not wasted if the hosted product ever happens. Read that doc for the
multi-tenant story and this one for getting a working instance on a real domain.

One correction to that doc before anything else: its claim that the dashboard has no authentication
is out of date. See the next section.

## The auth question, verified on current main

`docs/hosted-version-scope.md` says "there is no login page, no session handling, no middleware, no
`next-auth`, nothing." That was true when it was written and is not true now. Initiative 1 landed.
Verified on `main` at commit `18bef84`:

- `web/package.json` depends on `@clerk/nextjs` ^6.9.6 and `svix` ^1.42.0.
- `web/middleware.ts` runs `clerkMiddleware()` with `auth.protect()` over every route, leaving only
  `/sign-in`, `/sign-up` and `/api/webhooks/clerk` public, and redirecting a signed-in user with no
  active organization to `/select-org`.
- `web/app/layout.tsx` wraps the tree in `<ClerkProvider>`; `web/components/Shell.tsx` mounts
  `<OrganizationSwitcher>` and `<UserButton>`; there are real `web/app/sign-in`, `web/app/sign-up`
  and `web/app/select-org` pages, plus `web/app/settings/members` and `web/app/settings/keys`.
- `web/lib/getTenant.ts` resolves the active Clerk org to a tenant UUID through the gateway's
  `GET /v1/tenant/by-clerk-org/{orgId}`, and throws `NoActiveOrgError` rather than falling back to a
  pinned tenant.
- Roles exist at the coarse level `canManage()` enforces: `org:admin` may mint, rotate and revoke
  keys, everyone else may not.

`docs/initiatives/01-organizations-users-access.md` §13 is the accurate status map, and its
"Not built" list is the honest remainder: `organization.deleted` handling, `last_used_at` stamping,
plan and billing source, data-residency routing, Clerk custom roles, and constant-time compare
hardening in the edge proxy. None of those block a private instance.

**The trap that does matter.** There is one environment variable, `TALLY_DEV_TENANT`, that turns all
of the above off. When it is set, `web/middleware.ts` exports a pass-through, `layout.tsx` mounts no
`ClerkProvider`, `getTenant()` short-circuits to the pinned tenant, and `canManage()` returns true
unconditionally. It exists so `make up` and CI run without Clerk keys, and it is the correct design.
It is also a single unset-me-in-production variable standing between a public URL and every number
we have. `deploy/vercel/README.md` already says to leave it unset in production. That warning needs
to become a deploy-time assertion, not a line in a runbook.

So the auth work for reading A (a private instance) is small: buy or provision a Clerk production
instance, set the keys, leave `TALLY_DEV_TENANT` unset, and add a build-time or boot-time guard that
refuses to start a production deployment with it set. Restricting sign-up to our own email domain is
a Clerk configuration setting rather than code.

The work for reading B (a public demo) is not small, and that shapes the recommendation below.

## Which reading of "link it to my website"

**A. A private instance** at `tally.example.com`, linked from the site, behind Clerk sign-in, holding
our real AI spend. **B. A public live demo** any visitor can click into and explore.

**Recommendation: build A now. Do B later, separately, and never on the same instance.**

The reasons are concrete rather than aesthetic. B needs an unauthenticated read-only mode, which does
not exist today: the middleware protects every route and `getTenant()` throws without an org, so the
only way to serve an anonymous visitor today is `TALLY_DEV_TENANT`, which is exactly the switch that
disables the protection. Building a real anonymous read path means a public-route matcher, a
read-only role that the control-plane write endpoints reject, and rate limiting on the query side,
because ClickHouse dashboard queries are the expensive thing and an unauthenticated one is a free
denial-of-wallet. Beyond the engineering, a demo showing our real spend is a disclosure of our model
mix, our traffic volume, and our unit economics.

There is already a better answer for B, and it exists in the repo. `deploy/demo/` is a single-VM kit
that stands the whole stack up behind Caddy with automatic TLS and HTTP basic auth, seeded with an
explicitly synthetic dataset. `deploy/demo/deploy.sh` brings compose up, waits for gateway health,
applies the ClickHouse DDL, seeds and backfills, and prints the link; `reseed.sh` resets the data on
a cron; `lib-tenant.sh` does the tenant-UUID resolution and the service-token preflight both need.
That is the closest thing in the repo to a one-command deploy, and it is the right shape for a
shopfront: a separate host, separate data, synthetic numbers, and a shared password we hand out. It
is not the same machine as our production instance and should never become it.

## What already works today, verified

Confirmed by reading the files, not inferred.

The gateway is `infra/gateway/src/gateway/app.py`. It exposes three ingest surfaces:
`POST /v1/batches` (the native protocol), `POST /v1/otlp/traces` (OTLP over HTTP with JSON, with the
translation in `infra/gateway/src/gateway/protocol.py::otlp_traces_to_spans`), and `POST /v1/events`
for CDP-shaped events. All three run the same `_run_pipeline`, which enforces bearer API keys when
`TALLY_REQUIRE_API_KEY` is set, checks the key's `write`/`admin` scope, and rejects a body claiming a
different tenant than the key's. Key verification is a SHA-256 hash lookup against `api_keys.key_hash`
in Postgres (`infra/gateway/src/gateway/auth.py`).

The Python SDK sends over the native protocol. `tally.init()` in `sdk/python/src/tally/init.py` reads
`TALLY_KEY` and `TALLY_ENDPOINT`, and `BatchingTransport` in `sdk/python/src/tally/transport.py`
POSTs to `{endpoint}/v1/batches` on a daemon thread with a 512-span batch, a one-second flush, capped
exponential backoff, an `atexit` drain, and drop-oldest on a full 10,000-span buffer. It has no
required runtime dependencies (stdlib `urllib` only) and never raises into the caller's request path.
There is no OTLP exporter in the SDK; OTLP exists only on the gateway side for third-party OTel SDKs.

The Go edge proxy is `infra/edge-proxy/`, stdlib-only, listening on `:8088`, forwarding bodies
byte-for-byte through `httputil.ReverseProxy` and posting metadata-only telemetry to the gateway's
`/v1/batches`. It supports OpenAI, Anthropic and Gemini upstreams
(`infra/edge-proxy/internal/config/config.go`), and it consumes the `/v1/edge/keys` delta feed so key
verification in the hot path needs no round-trip.

S3 replay storage is real: `S3ReplayBlobStore` in `infra/gateway/src/gateway/replay_store.py`,
selected by `replay_blob_backend=s3` and configured by `replay_s3_bucket`, `replay_s3_prefix`,
`replay_s3_region` and `replay_s3_endpoint` in `infra/gateway/src/gateway/config.py`. boto3 is an
optional `[s3]` extra and the client uses the AWS default credential chain, so no access key is
handled in code.

Deploy artifacts exist for the gateway and the web tier on AWS: `deploy/aws/ecs/gateway.taskdef.json`,
`web.taskdef.json`, the matching service definitions, and IAM policies under `deploy/aws/ecs/iam/`,
with a from-zero runbook in `deploy/aws/README.md`. `deploy/vercel/README.md` is a complete runbook
for the dashboard on Vercel including the exact environment variables. `deploy/aws/helm/ai-tally-eks/`
is the EKS alternative.

The per-tenant HMAC reference is enforced in the schema. `db/postgres/0001_control_plane.sql` holds
`tenants.hash_salt_kek_ref TEXT NOT NULL` with
`CONSTRAINT no_raw_secret CHECK (hash_salt_kek_ref NOT LIKE 'sk-%' AND length(hash_salt_kek_ref) < 512)`.

`db/clickhouse/otel_spans.sql` carries a real TTL: warm at 7 days, cold at 30, delete at 90,
rendered from `sdk/python/src/tally/storage_tiering.py`.

### What is aspirational, and should not be credited

**There is no deploy artifact for the edge proxy anywhere under `deploy/`.** `deploy/aws/ecs/` has
only `gateway.*` and `web.*`; neither Helm chart has an edge-proxy template; it is not in
`infra/docker-compose.yml`. The only chart is inside the component directory at
`infra/edge-proxy/deploy/helm/edge-proxy/`. Running the proxy on ECS Fargate means writing a task
definition, a service definition and a target group that do not exist yet. That is the single largest
undone piece of the chosen architecture.

**The production HMAC key provider is a stub.** `SecretManagerKeyProvider` in
`infra/gateway/src/gateway/tenant_provisioning.py` raises
`NotImplementedError("wire the deployment's Secret Manager / KMS client")` on every method. Only
`LocalKeyMaterialProvider` works, deriving key material in process from a root secret. No AWS ARN
appears anywhere in the gateway or the migrations. The invariant that identifiers are hashed under a
per-tenant key holds; the invariant that the key lives in Secrets Manager does not hold yet in code.
For a single-tenant private instance the local provider with a root secret injected from Secrets
Manager is defensible, but it should be a recorded decision rather than an accident.

**There is no migration runner.** See the operational burden section.

**Nobody has run this on ECS end to end**, as far as I can tell from the repo. The task definitions
are checked in and the runbook is detailed, but I found no evidence of an executed deploy. Treat
`deploy/aws/` as a well-researched plan rather than a proven path, and budget accordingly.

## Target architecture

Vercel for web, AWS for everything stateful, decided 2026-09-03. Keep the Vercel project's function
region and every AWS resource in the same region so the dashboard's ClickHouse queries and gateway
calls do not cross a continent on every page load.

| Component | Service | Notes |
| --- | --- | --- |
| Dashboard | Vercel, Next.js 15 | `web/vercel.json` and `deploy/vercel/README.md` already cover it. Root directory `web/`, Node 22. |
| Ingest gateway | AWS ECS Fargate behind an ALB | `deploy/aws/ecs/gateway.taskdef.json`. Can scale to zero-ish; latency is not in anyone's request path. |
| Edge proxy | AWS ECS Fargate, kept warm | No task definition exists yet. It sits in the LLM hot path with a p99 budget under 3ms, so no cold starts and no scale-to-zero. |
| Telemetry store | ClickHouse Cloud | Must be a public HTTPS endpoint on 8443, because Vercel functions egress to the public internet. |
| Control plane | RDS for PostgreSQL | Single small instance. Only the gateway talks to it; the dashboard never does. |
| Replay bodies | S3 | `S3ReplayBlobStore`. Set a bucket lifecycle policy; the code deliberately does not manage object TTL. |
| Secrets | AWS Secrets Manager, KMS for the HMAC root | Injected into the ECS task via the task role, per `deploy/aws/ecs/iam/`. |
| Identity | Clerk production instance | Restrict sign-up to our own domain. |

Redpanda and MinIO from `infra/docker-compose.yml` are local-development conveniences and are not in
this architecture; MinIO is replaced by S3 and the queue is not required for this shape.

### Cost

I am not going to invent numbers. This is a product whose premise is that a fabricated cost figure is
worse than a blank, and the same rule applies to its own scope doc. Every line below is either a
meter I can name or an honest **unknown**.

Vercel: **unknown**. The Hobby tier may cover a single low-traffic dashboard, but a commercial site
almost certainly requires Pro, and the Route Handlers run on the Node runtime so function invocation
and duration are metered on top. Pricing inputs: seat count, function invocations, function GB-hours,
and egress.

ECS Fargate for the gateway and the proxy: **unknown in dollars, derivable from a known formula.**
Fargate bills vCPU-seconds and GB-seconds per running task. The inputs are the task size in the
`deploy/aws/ecs/*.taskdef.json` files, the desired count, and the current regional per-vCPU-hour and
per-GB-hour rates, which I do not have. Two always-on tasks plus the ALB's hourly charge and its LCU
charge is the shape of the bill. The proxy stays warm by design, so it is a floor rather than a
variable.

ClickHouse Cloud: **unknown, and the largest single line.** It meters compute (a minimum always-on
service size, unless the development tier's idling is acceptable) and compressed storage. The
90-day DELETE TTL in `db/clickhouse/otel_spans.sql` bounds the storage growth, which is the one
number under our control.

RDS Postgres: **unknown.** The control plane is small and low traffic; the meter is instance-hours
plus allocated storage plus backup storage over the free allotment. A single small instance without
Multi-AZ is the honest starting point for a private tool.

S3 and Secrets Manager: **small but not zero.** S3 charges per GB-month plus requests, and only if
replay sampling is turned on at all. Secrets Manager charges per secret per month plus API calls.

Clerk: **unknown.** Free below a monthly-active-user threshold, but organizations are a paid feature
on some plans and this deployment depends on organizations. Check before assuming zero.

The correct next step is to price these against current rate cards, not to guess. If the total
matters more than the operational simplicity, the honest cheaper alternative is the single-VM shape
in `deploy/demo/`, which trades managed backups and elasticity for one instance running everything
and is defensible for a private tool serving a handful of people.

## The data path, end to end

Our application emits spans. The spans reach `https://ingest.<our-domain>/v1/batches` on the ALB in
front of the gateway task, authenticated by a `tally_sk_live_` bearer key minted from
`web/app/settings/keys`. The gateway authenticates the key, rate limits, deduplicates on the batch
id, validates, enriches with pricing, and writes rows to ClickHouse stamped with our tenant UUID.
The dashboard on Vercel queries ClickHouse directly over HTTPS for telemetry and calls the gateway
for control-plane reads and writes with the `GATEWAY_SERVICE_TOKEN` and an `x-tenant-id` header.

**For a first install, use the SDK.** Three reasons. It is the only path with zero deployment
surface: `pip install`, `tally.init()`, one endpoint and one key, and the batching, retry and drain
behaviour is already written and tested. It is the only path that gets the HMAC bootstrap for free,
since `init.py` fetches `/v1/tenant/hmac-key` off-thread. And it fails safe, dropping spans rather
than raising into the caller.

Adopt the edge proxy second, once the SDK path is proving out numbers, because it is the only path
that catches spend from services not written in Python, and because it is the one component with no
deploy artifact and a latency budget to defend. Do not make the first install depend on it.

Use OTLP only if we already run an OpenTelemetry collector and would rather add an exporter than a
dependency. It works (`POST /v1/otlp/traces`), but it is a translation layer, and a translation
layer is a worse place to debug a missing first span than a purpose-built SDK.

## Linking it to the website

Use a subdomain, not a path. A path under the marketing site would mean putting a reverse proxy in
front of a Next.js app that expects to own its routes, and the two have different deploy cadences,
different auth, and different blast radius. Three records:

`tally.example.com` points at Vercel (a CNAME to the Vercel target, or an A record if the apex is
involved), and Vercel issues and renews the certificate. `ingest.example.com` points at the ALB in
front of the gateway, with the certificate issued by ACM in the same region. If the edge proxy is
deployed, `llm.example.com` gets a third record and a second ALB listener, and it is the one that
must never be publicly discoverable, because it forwards to provider APIs with our keys.

**CORS is a real gap.** There is no `CORSMiddleware` anywhere in `infra/gateway/src/`, and no
`Access-Control-*` header is ever set. This is fine today because the dashboard's Route Handlers call
the gateway server to server, not from the browser. It stops being fine the moment anything in a
browser calls the gateway directly, including a browser-side SDK. If we ever want that, CORS has to
be added deliberately with an origin allowlist rather than a wildcard, and it should be tracked as a
ticket rather than discovered as a bug.

What the website links to is a plain `<a href="https://tally.example.com">` in the site nav or
footer. An unauthenticated visitor hits Clerk's sign-in page and stops there. That is the whole of
reading A, and it is the correct behaviour.

## Ongoing operational burden

**Migrations are the sharpest edge.** There is no migration runner and no table recording which
migrations have run. `db/postgres/` holds `0001` through `0032` with `0005` used twice and `0017`
never allocated, and `infra/docker-compose.yml` mounts 28 of the 32: `0009`, `0010`, `0020` and
`0021` are unmounted and have never run anywhere. Worse, the mount mechanism is
`/docker-entrypoint-initdb.d/`, which fires only on a first boot against an empty data directory, so
an existing stack never picks up a new migration no matter how often it restarts. The branch
`fix/migration-sequence` documents all of this in `db/postgres/README.md` and proposes a
`make pg-migrate` target that replays the directory in `LC_ALL=C` order and stops on first failure.
That branch is not merged. On RDS none of the compose mounting applies at all, so a self-hoster on
the chosen architecture has to apply 32 files by hand in the right order, and get the `0005`
ordering right, with nothing recording what succeeded. **Land the migration runner before the first
production deploy.** This is the one prerequisite I would not skip.

ClickHouse is in better shape. `make ch-migrate` from `infra/` replays the DDL against a running
stack and the DDL is idempotent, but two migrations are deliberately excluded from it and are
one-shot manual operations (`ch-migrate-otel-engine` and the rollup rebuild), because ClickHouse
cannot alter a table's engine or sort order in place. Retention is the 7/30/90 day TTL on
`otel_spans` only; the rollup and attribution tables have no TTL by design, so they grow without
bound and should be watched.

Backups: RDS automated backups cover the control plane, ClickHouse Cloud covers telemetry on its own
schedule, and S3 versioning plus a lifecycle policy covers replay blobs. None of that is a restore
until someone has actually restored it once, on purpose, and written down how long it took.

Secret rotation: ingest keys rotate from `web/app/settings/keys` through
`POST /v1/tenant/keys/{id}/rotate`, which mints and revokes in one transaction, so that path is
solved. The HMAC root and the `GATEWAY_SERVICE_TOKEN` have no rotation story at all today. Rotating
the HMAC key changes every user hash, which breaks historical joins, so it needs the key-version
mechanism (`tenant_hmac_key.py` already parses a version selector off the reference) to be exercised
rather than assumed.

Upgrades: Vercel redeploys on push to `main` per `web/vercel.json`. The gateway and proxy are new
task definition revisions and an ECS service update, which is a rolling replace with health checks.
The risk in an upgrade is not the containers, it is a schema change arriving without a runner to
apply it, which is the same problem as above.

## Phased plan

**Phase 0, a weekend.** Prove the whole shape on the single-VM kit before touching AWS. Follow
`deploy/demo/README.md`: a 4 vCPU / 8-16 GB VM, DNS for one subdomain, a filled-in `.env`, and
`./deploy/demo/deploy.sh`. That gives a real TLS URL with the synthetic dataset behind basic auth,
end to end, in an afternoon, and it answers the questions that matter (does the dashboard render,
does ingest work, what does it feel like) before any of the AWS work. This is also, unmodified, the
public demo host for reading B later.

**Phase 1.** Land the migration runner (rebase and merge `fix/migration-sequence`), and add the guard
that refuses to boot a production web deployment with `TALLY_DEV_TENANT` set. Neither is large;
both are prerequisites for anything durable.

**Phase 2.** Stand up the managed stores: ClickHouse Cloud, RDS, the S3 bucket with a lifecycle
policy, and the Secrets Manager entries. Apply the schema with the runner from phase 1.

**Phase 3.** Deploy the gateway to ECS Fargate from `deploy/aws/ecs/gateway.taskdef.json`, behind an
ALB with an ACM certificate on `ingest.example.com`. Turn `TALLY_REQUIRE_API_KEY` on. Mint a key.

**Phase 4.** Deploy the dashboard to Vercel on `tally.example.com` with a Clerk production instance,
sign-up restricted to our domain, and `TALLY_DEV_TENANT` unset. Link it from the website. Reading A
is done at the end of this phase.

**Phase 5.** Instrument our own application with the Python SDK and watch real spans land.

**Phase 6.** Write the edge-proxy ECS task definition and deploy it warm, if and only if we have
non-Python services worth metering.

## Open questions and risks

I could not verify that anyone has run `deploy/aws/ecs/` end to end. The artifacts and the runbook
are thorough, but a checked-in task definition is not a proven deploy, and the estimate for phase 3
should assume first-run friction.

The HMAC provider question needs a decision, not a default. `SecretManagerKeyProvider` is a stub, so
either we implement it against AWS KMS or we consciously accept `LocalKeyMaterialProvider` with a
root secret injected from Secrets Manager. For one tenant the second is reasonable. It should be
written down.

Every cost line in this doc is unknown in dollars. That is a real gap in the scope, not a stylistic
choice, and it should be closed by pricing the components against current rate cards before anyone
commits to the architecture.

Whether Vercel is worth it for one dashboard is worth asking. The web tier already has a working
`web/Dockerfile` and an ECS task definition, so running it beside the gateway on Fargate would remove
a vendor, remove the public-internet egress requirement that forces ClickHouse Cloud to have a public
endpoint, and let everything sit in one VPC. The decision is made and this doc follows it, but the
reason to keep Vercel is deploy ergonomics, not cost or latency, and if the ergonomics stop paying
for themselves the alternative is already built.

Requiring a public ClickHouse endpoint is a consequence of that decision and a standing risk. It is
mitigated by TLS, a strong password and ClickHouse Cloud's IP allowlist, but the allowlist is awkward
against Vercel's egress ranges, so it may end up effectively open to the internet with only
credentials in front of it. Confirm the allowlist story before phase 2 rather than after.

Retention on the rollup and attribution tables is unbounded today. For a private instance that is a
slow leak rather than a fire, but it is a cost line that grows on its own, and nobody is watching it.
