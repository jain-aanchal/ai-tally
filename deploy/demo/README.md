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

## Operator runbook

### 1. Provision a VM

Any Docker-capable Linux VM works (DigitalOcean, Hetzner, GCP, Fly, AWS EC2, ...). ClickHouse likes
RAM, so size for it:

- **4 vCPU / 8-16 GB RAM**, ~40 GB disk.
- Open inbound TCP **80** and **443** only. Nothing else needs a public port.

Install Docker Engine + the Compose plugin (Docker's official convenience script is fine):

```
curl -fsSL https://get.docker.com | sh
```

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

- Do **not** set the tenant by hand. The dashboard reads `TALLY_DEV_TENANT`, and it must hold the
  tenant **UUID**, not the name `local-dev`: the web binds that value straight into the ClickHouse
  read filter (`TenantId = ...`) and the backfill tags spans with the UUID, so a name matches no
  rows and you get an empty dashboard with no error. The UUID only exists after seeding, so
  `deploy.sh` resolves it from Postgres, hands it to the backfill, and recreates the web service
  with it. If resolution ever fails, both scripts abort with the reason instead of falling back.

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

### 5. Share privately

Send the link and the shared password to testers **privately** (DM / password manager share). This
beta is **private, not open** - the single basic-auth login is the only gate, so treat it like a
password.

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

All three must show the same **UUID**. A `local-dev` (the name) in either of the last two means
something bypassed `deploy.sh`; re-running `./deploy/demo/reseed.sh` re-resolves and repairs it.

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
