# ai-tally demo-deploy-kit

Drop-in kit to host the **whole ai-tally stack on one cloud VM**, behind Caddy (automatic HTTPS +
HTTP basic-auth), so testers get a single private link to the seeded demo. (CTO-243)

Everything runs from `docker compose`, layered on top of the existing local stack:

```
docker compose -f infra/docker-compose.yml -f deploy/demo/docker-compose.prod.yml up -d --build
```

The overlay adds a `web` (Next.js dashboard) and a `caddy` service, and makes **Caddy the only
service that publishes host ports** (80/443). ClickHouse, Postgres, Redpanda, MinIO and the gateway
keep talking to each other over the compose network but are not reachable from the internet.

> The demo dataset is **synthetic** - seeded and backfilled by this kit (no real users, no real LLM
> calls, no API keys). See `deploy.sh` / `reseed.sh`.

## What's in here

| File | Purpose |
|------|---------|
| `web.Dockerfile` | Multi-stage build of the Next.js dashboard (standalone, `node:22`), root build context. |
| `docker-compose.prod.yml` | Overlay: adds `web` + `caddy`, strips host ports off the base services. |
| `Caddyfile` | `${DOMAIN}` site: automatic TLS, basic-auth, `reverse_proxy web:3000`. Commented gateway-ingest and local-HTTP variants. |
| `.env.example` | Per-host config: domain, basic-auth user + bcrypt hash, stack creds, service token. |
| `deploy.sh` | Bring the stack up, wait for health, apply DDL, seed + backfill, print the link. |
| `reseed.sh` | Reset + re-seed the synthetic data (run nightly via cron). |
| `lib-tenant.sh` | Sourced by both scripts: tenant-UUID resolution and the service-token preflight. |

## Two modes: `basic` and `clerk`

This kit was written to host a synthetic-data demo, and that is still the default. CTO-367 adds a
second shape for a real instance. They are mutually exclusive, chosen by `AUTH_MODE` in `.env`.

| | `basic` (default) | `clerk` |
|---|---|---|
| Dashboard auth | **off** (`TALLY_DEV_TENANT` pins the tenant) | real Clerk sign-in |
| What guards it | Caddy HTTP basic-auth | Clerk; Caddy is TLS only |
| Tenant | the seeded demo tenant | resolved from the signed-in organization |
| Data | synthetic backfill | whatever the tenant has |
| `.env` needs | `BASIC_AUTH_USER`, `BASIC_AUTH_HASH` | the four Clerk values |

The exclusivity is the point rather than a convenience. `TALLY_DEV_TENANT` does not only pin a
tenant, it turns the dashboard's authentication off completely, so setting it in `clerk` mode would
leave an instance you believe is protected serving every number to anyone who reaches the URL.
`deploy.sh` sets one or the other, never both.

### Why `clerk` mode drops basic-auth rather than keeping both

Clerk's flows are redirects: its hosted sign-in page, the OAuth round trip to your identity
provider, and the `organization.created` webhook Clerk POSTs to `/api/webhooks/clerk`. A basic-auth
challenge in front of those either breaks them or asks the user for two unrelated credentials.

The webhook is the one that fails quietly. Clerk gets a 401, retries, gives up. No tenant is ever
provisioned, and the symptom is a signed-in user whose workspace never appears, which reads as a
broken product rather than a misconfigured proxy.

### Running in `clerk` mode

1. Create a **production** Clerk instance (development instances only work against `localhost`),
   verify its DNS, and supply your own Google OAuth credentials if you want social sign-in.
   Production instances do not get Clerk's shared ones.
2. Put `AUTH_MODE=clerk`, the publishable key, the secret key and `GATEWAY_SERVICE_TOKEN` in `.env`.
3. Run `./deploy.sh`. It will warn that the webhook secret is missing, which is expected: the
   endpoint cannot exist until this deployment has a URL.
4. In Clerk, add a webhook at `https://${DOMAIN}/api/webhooks/clerk` subscribed to
   `organization.created` only. Put the `whsec_` value in `.env` as
   `CLERK_WEBHOOK_SIGNING_SECRET` and re-run `./deploy.sh`.
5. Sign up, create an organization, and you get a fresh empty tenant. To put that organization in
   front of the seeded demo corpus instead, use `gateway.adopt_org`; see
   [docs/runbook-tenants.md](../../docs/runbook-tenants.md).

Run `make prod-preflight` from `infra/` before step 3. It checks both sides of the configuration and
names the symptom each missing value produces.

### What this kit is not

One VM, one disk, no replicas and no managed backups. That is a reasonable first production instance
and a poor long-term one. The retention policy applied by `make ch-apply-retention` is the only data
lifecycle here, so put a snapshot schedule on the volume. `deploy/aws/terraform/` is the managed
alternative when you outgrow this.

## Operator runbook

### 1. Provision a VM

Any Docker-capable Linux VM works (DigitalOcean, Hetzner, GCP, Fly, AWS EC2, ...). ClickHouse likes
RAM, so size for it:

- **4 vCPU / 8-16 GB RAM**, ~40 GB disk.
- Open inbound TCP **80** and **443** only. Nothing else needs a public port.

Install Docker Engine + the Compose plugin (Docker's official convenience script is fine), and
`make`, which deploy.sh uses for the ClickHouse DDL and seed targets in `infra/Makefile`:

```
curl -fsSL https://get.docker.com | sh
apt-get update && apt-get install -y make
```

`make` is worth calling out because it is NOT part of a Docker install. DigitalOcean's Docker
marketplace image does not ship it, and neither does a minimal Ubuntu. deploy.sh now checks for it
before building anything rather than failing three minutes in.

Then clone this repo onto the VM (e.g. into `/opt/ai-tally`).

### 2. Point DNS at the VM

Create a DNS **A record** for your `${DOMAIN}` (e.g. `demo.example.com`) pointing at the VM's public
IP. Caddy needs this resolvable to obtain a Let's Encrypt certificate over the port-80 ACME
challenge. Wait for it to propagate before the first deploy.

### 3. Configure `.env`

```
cp deploy/demo/.env.example deploy/demo/.env
```

Edit `deploy/demo/.env`:

- Set `DOMAIN` to your record.
- Set `BASIC_AUTH_USER` (e.g. `tester`).
- Generate the bcrypt hash for the shared password and paste it into `BASIC_AUTH_HASH`:

  ```
  docker run --rm caddy:2 caddy hash-password --plaintext 'yourpassword'
  ```

  Store only the `$2a$...` hash in `.env`; keep the plaintext to share with testers. `.env` is
  host-specific and is git-ignored - do not commit it.

- Leave `TALLY_GATEWAY_SERVICE_TOKEN` commented out unless you also turn gateway auth on
  (`TALLY_REQUIRE_API_KEY=true`). If you do turn it on, generate a real token and never commit it:

  ```
  echo "TALLY_GATEWAY_SERVICE_TOKEN=$(openssl rand -hex 32)" >> deploy/demo/.env
  ```

  One key covers both tiers: the compose overlay hands the same value to the gateway as
  `TALLY_GATEWAY_SERVICE_TOKEN` and to the web server as `GATEWAY_SERVICE_TOKEN`. With auth on and
  the token empty the gateway refuses to boot rather than serve an open control plane, so
  `deploy.sh` and `reseed.sh` stop up front and say so.

  **This kit's seeding path does not support auth on.** The synthetic backfill
  (`examples/vercel-chatbot/scripts/backfill-spans.ts`) posts to `/v1/batches` with no
  `Authorization` header and has no api-key option, and with `TALLY_REQUIRE_API_KEY=true` the
  gateway answers `401`. The stack and dashboard still come up, but the backfill step fails and you
  get no demo data. Seed with auth off, then turn auth on afterwards if you need it. Both scripts
  warn about this before they do any work rather than letting you discover it minutes in.

- Do **not** set the tenant by hand. The dashboard reads `TALLY_DEV_TENANT`, and it must hold the
  tenant **UUID**, not the name `local-dev`: the web binds that value straight into the ClickHouse
  read filter (`TenantId = ...`) and the backfill tags spans with the UUID, so a name matches no
  rows and you get an empty dashboard with no error. The UUID only exists after seeding, so
  `deploy.sh` resolves it from Postgres, hands it to the backfill, and recreates the web service
  with it. If resolution ever fails, both scripts abort with the reason instead of falling back.

- **This kit runs with the dashboard's authentication switched OFF, on purpose.** `TALLY_DEV_TENANT`
  does not only pin a tenant: with it set there is no Clerk middleware, no `ClerkProvider`, and
  every visitor is treated as an org admin who can mint, rotate and revoke API keys. That is fine
  *here*: the data is synthetic, the host is separate, and Caddy basic auth is the access control.
  It is a disaster on an instance holding real tenant data, and this kit is the most copyable thing
  in the repo, so the web image now **refuses to boot** on `TALLY_DEV_TENANT` alone in a production
  build. Turning auth off takes a second, deliberate variable, `TALLY_ALLOW_INSECURE_NO_AUTH=1`,
  which `deploy.sh` and `reseed.sh` set for you (`pin_dashboard_tenant` in `lib-tenant.sh`) and
  which every boot then warns about in the web container's logs. **If you are adapting this kit to
  stand up a real instance, delete both variables and configure Clerk** (see `deploy/aws/README.md`
  or `deploy/vercel/README.md`); do not carry them across.

### 4. Deploy

```
./deploy/demo/deploy.sh
```

This builds the images, starts the stack, waits for the gateway to be healthy, applies the
ClickHouse DDL (including `replay_samples`), seeds the tenant, resolves that tenant's UUID and
points the dashboard at it, then backfills 30 days of synthetic spans. When it finishes it prints:

```
URL:   https://demo.example.com
Login: tester  (password: the plaintext you hashed)
```

Re-running `deploy.sh` (to pick up a code change, say) does **not** re-post the backfill: it counts
the tenant's existing spans in ClickHouse first and skips the step when there are any. Two overrides:

| Variable | Effect |
| --- | --- |
| `SKIP_BACKFILL=1` | Never back-fill, even on an empty tenant. |
| `FORCE_BACKFILL=1` | Back-fill anyway, on top of what is already there. |

Use `./deploy/demo/reseed.sh` rather than `FORCE_BACKFILL=1` when you want a clean dataset: it
truncates first, so the spans are replaced instead of doubled.

### 5. Share privately

Send the link and the shared password to testers **privately** (DM / password manager share). This
beta is **private, not open** - the single basic-auth login is the only gate, so treat it like a
password.

## Hosted edge proxy: zero-code connect (optional, off by default)

The stack can also run the Go edge proxy as a hosted service on its own hostname, so a customer can
start metering by changing one base URL instead of installing the SDK. It is **off unless
`INGEST_DOMAIN` is set**: off means no proxy container and no Caddy site, not a proxy that rejects
traffic. Turn it off again by removing `INGEST_DOMAIN` and re-running `deploy.sh`, which removes the
site; stop the container with `docker compose ... --profile ingest stop edge-proxy`.

### One-time setup

1. Create a DNS **A record** for the ingest hostname (e.g. `ingest.ai-tally.com`) pointing at the
   same VM as `${DOMAIN}`. Caddy issues its certificate on first request, so the record has to
   resolve before you deploy.
2. In `.env`, set `INGEST_DOMAIN=ingest.ai-tally.com` and make sure `TALLY_GATEWAY_SERVICE_TOKEN`
   is set (`openssl rand -hex 32`, generated on the box). With `INGEST_DOMAIN` set and no token,
   `deploy.sh` stops before building.
3. Run `./deploy/demo/deploy.sh`. It enables the `ingest` compose profile, mounts the ingest Caddy
   site, and prints the endpoint.

No firewall change is needed: the proxy has no host port and is reached only through Caddy on 443.

### What a customer configures

They create a key in the dashboard under Settings > API keys, then point their client at the
matching prefix and add one header. Their own provider key is sent exactly as before and forwarded
untouched; ai-tally never stores it.

| Provider | Base URL | Header |
|---|---|---|
| OpenAI | `https://ingest.ai-tally.com/openai/v1` | `X-Tenant-Key: tally_sk_...` |
| Anthropic | `https://ingest.ai-tally.com/anthropic` | `X-Tenant-Key: tally_sk_...` |
| Gemini | `https://ingest.ai-tally.com/gemini` | `X-Tenant-Key: tally_sk_...` |

Optional headers: `X-Tally-Feature-Tag` (which feature made the call) and `X-Tally-Account-Id-Hash`
(an already-hashed customer id, for cost per customer). OpenAI streaming reports token usage only
when the client sets `stream_options: {"include_usage": true}`.

### How it behaves

- **Unknown or revoked key: `403`, never forwarded.** The proxy runs with
  `EDGE_PROXY_REQUIRE_TENANT=true`, so the hostname is not an open relay.
- **The key needs `write` or `admin` scope.** Metering a call writes spans, so a `read` key is
  refused with `403 tenant key lacks write scope`, the same rule the gateway applies to
  `/v1/batches`. Settings > API keys creates `write` keys by default.
- **A brand-new key can take up to 45 seconds to work.** Keys resolve from an in-memory cache of the
  gateway's key feed, refreshed every 45s, so no gateway call sits in the request path. Revocation
  propagates on the same interval.
- **Anything except `/openai/*`, `/anthropic/*`, `/gemini/*` and `/healthz` is a `404` from Caddy.**
  The ingest hostname cannot reach the dashboard or the gateway.
- **Spans reach the dashboard through the internal network.** The gateway's `/v1/batches` stays
  unpublished.
- **If the proxy cannot load keys at startup, it exits rather than serving.** Resolution fails
  closed, so a proxy that could not read the key feed does not come up accepting traffic it cannot
  authenticate. Compose waits for the gateway to be healthy first and restarts the proxy, so a
  transient failure recovers on its own. A proxy stuck restarting usually means
  `TALLY_GATEWAY_SERVICE_TOKEN` does not match between the gateway and the proxy while
  `TALLY_REQUIRE_API_KEY` is on: check `docker logs ai-tally-edge-proxy`. After a successful start,
  a feed error keeps the last good key map instead of failing.

### Check it after a deploy

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://ingest.ai-tally.com/healthz
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Tenant-Key: tally_sk_bogus' https://ingest.ai-tally.com/openai/v1/models
```

The first should print `200` and the second `403`. For token counts checked against real provider
usage, run `docs/real-traffic-verification.md` (#350) before onboarding customers.

### What this is not

**It is one container on one VM in the synchronous path of your customers' LLM calls.** When the
droplet is down, their AI features fail, not just the dashboard. That is acceptable for pilot
customers who have been told, and not for a broad launch, which wants at least two instances behind
a load balancer (`deploy/aws/terraform` builds that). Latency: the proxy itself adds well under 3ms,
but the round trip through this VM's region adds a network hop that depends on where the customer
runs.

## Security posture

- **Only Caddy is public.** It publishes 80/443; every other service has its host ports removed by
  the overlay and is reachable only over the internal compose network.
- **The gateway stays internal behind auth.** It carries write endpoints (`/v1/batches` ingest,
  control-plane `/v1/tenant/*`), so it must not be exposed directly. The dashboard reaches it as
  `http://gateway:8080` server-side; the browser only ever talks to the dashboard.
- **Basic-auth on everything.** Caddy challenges every request, so the dashboard is never anonymous.
- Use a strong shared password and rotate it (re-hash, update `.env`, `docker compose ... up -d
  caddy`) if it leaks.

## Exposing the gateway ingest (optional)

If testers should send **their own** telemetry, expose only the ingest endpoint (still behind the
same basic-auth) by uncommenting the `handle /v1/batches*` block in the `Caddyfile`, then:

```
docker compose -f infra/docker-compose.yml -f deploy/demo/docker-compose.prod.yml up -d caddy
```

Point their SDK / edge-proxy at `https://${DOMAIN}/v1/batches` with the basic-auth credentials. Do
**not** publish a host port for the gateway; keep it behind Caddy.

## Resetting the data

The demo data is synthetic and backdated relative to "now", so re-running keeps the window current.

- **On demand:** `./deploy/demo/reseed.sh` truncates the telemetry tables and re-seeds + re-backfills.
- **Nightly:** add a cron entry (see the comment block in `reseed.sh`):

  ```
  15 3 * * * /opt/ai-tally/deploy/demo/reseed.sh >> /var/log/ai-tally-reseed.log 2>&1
  ```

## Local smoke test (no VM, no TLS)

You can validate the pieces on a laptop without binding 80/443 or owning a domain:

```
# Config resolves:
docker compose -f infra/docker-compose.yml -f deploy/demo/docker-compose.prod.yml config

# The dashboard image builds:
docker build -f deploy/demo/web.Dockerfile -t ai-tally-web-demo .

# Caddy config is valid:
docker run --rm -v "$PWD/deploy/demo/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -e DOMAIN=:8088 -e BASIC_AUTH_USER=tester -e BASIC_AUTH_HASH='<hash>' \
  caddy:2 caddy validate --config /etc/caddy/Caddyfile
```

For an end-to-end auth check, run Caddy with the commented `:8088` HTTP block from the `Caddyfile`
proxying to a `web` container on a spare port and `curl` it: no creds -> `401`, correct creds ->
`200`.

## Troubleshooting

**The dashboard renders but every panel is empty.** Almost always a tenant mismatch: the spans were
written under one `TenantId` and the dashboard is reading another. Check the two ends agree:

```
# What the control plane says the tenant UUID is:
docker compose -f infra/docker-compose.yml -f deploy/demo/docker-compose.prod.yml \
  exec -T postgres psql -U tally -d tally -tAc "SELECT id, name FROM tenants"

# What the web tier is actually reading:
docker compose -f infra/docker-compose.yml -f deploy/demo/docker-compose.prod.yml \
  exec -T web printenv TALLY_DEV_TENANT

# What ClickHouse actually holds:
docker compose -f infra/docker-compose.yml -f deploy/demo/docker-compose.prod.yml \
  exec -T clickhouse clickhouse-client -u tally --password tally -d default \
  --query "SELECT TenantId, count() FROM otel_spans GROUP BY TenantId"
```

(Those commands assume the `.env.example` defaults `POSTGRES_USER=tally` / `POSTGRES_DB=tally` and
`CLICKHOUSE_USER=tally`. If you changed them in `deploy/demo/.env`, substitute your own values; the
scripts themselves read the env vars, only these copy-paste one-liners are literal.)

All three must show the same **UUID**. A `local-dev` (the name) in either of the last two means
something bypassed `deploy.sh`; re-running `./deploy/demo/reseed.sh` re-resolves and repairs it.

**The `web` container restarts in a loop and its logs say "REFUSES TO START".** The image was
started with `TALLY_DEV_TENANT` set but without `TALLY_ALLOW_INSECURE_NO_AUTH`, so it stopped rather
than serve with authentication disabled. That happens when something bypassed the scripts, typically
a bare `docker compose up` with a stale `TALLY_DEV_TENANT` still exported in the shell. Re-run
`./deploy/demo/deploy.sh` (or `./deploy/demo/reseed.sh`), which set both variables together. If you
are adapting this kit to serve REAL data, that message is telling you the truth: unset both and
configure Clerk instead.

**A control-plane write from the dashboard returns 401.** Gateway auth is on and the two tiers hold
different tokens. They come from the single `TALLY_GATEWAY_SERVICE_TOKEN` key in `.env`, so confirm
`.env` has it and re-run `deploy.sh`; a hand-edited container env is the usual cause.

## Feedback

Share a lightweight feedback link alongside the demo (a Google Form, Canny board, or a Linear
intake link) so testers can report what they see. Keep it in the same private message as the
credentials.

## Notes

- Runs on any Docker-capable VM; per-host specifics (domain, password, creds) live in `.env`.
- This kit does not modify `infra/docker-compose.yml` or the app; it is additive under
  `deploy/demo/`.
- The `chatbot-demo-backfill` make target runs on the host and hits `localhost:8080`; on a
  locked-down VM (no host Node, gateway not published) `deploy.sh`/`reseed.sh` instead run the same
  backfill script inside a throwaway `node:22` container attached to the compose network.
