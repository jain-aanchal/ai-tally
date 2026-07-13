# Deploying the ai-tally dashboard on Vercel

The `web/` app is a Next.js 15 / React 19 dashboard — Vercel is its most natural host. This runbook
makes deploying it a documented, one-command path. It is **additive**: it does not touch
`web/next.config.mjs`, the app source, `infra/docker-compose.yml`, or the local dev flow, and it is
independent of the [GCP](../gcp/README.md) and Docker paths (the same app, a different host).

```
 browser ──▶ Vercel (Next.js serverless) ──Route Handler──┐
                                                           │  queries ClickHouse live (HTTP/S)
                                                           │  calls the gateway (HTTP/S)
                                                           ▼
                                    ClickHouse (ClickHouse Cloud / public host)  +  gateway
```

The dashboard's Route Handlers run on the Node.js runtime and read two backing services:

- **ClickHouse** over HTTP(S) — the cost/telemetry queries (`web/lib/clickhouse.ts`).
- **the ingest gateway** over HTTP(S) — reconciliation / integrations / guardrails / replay
  (`TALLY_GATEWAY_URL`).

> **The dashboard does not connect to Postgres directly.** Postgres is the *gateway's* control plane;
> the web tier reaches that data through the gateway's HTTP API. So there is **no** `POSTGRES_URL` /
> `DATABASE_URL` to set on Vercel — only the ClickHouse and gateway vars below. (Deploying the gateway
> and stores themselves is out of scope here — see the [GCP](../gcp/README.md) / AWS deploys.)

## Out of scope

- Deploying the gateway / edge-proxy / backing stores (that's the cloud deploys).
- Custom domains / SSO.

---

## 1. Connect the repo

1. In the Vercel dashboard: **Add New… → Project**, import this Git repository.
2. **Root Directory → `web/`.** This is the single most important setting — it tells Vercel the
   Next.js app lives in `web/`, not the repo root. Vercel then reads `web/vercel.json`,
   `web/package.json`, and `web/next.config.mjs`.
3. **Framework Preset:** Next.js (auto-detected). `web/vercel.json` already pins the build/install
   commands, so no manual override is needed:

   ```json
   {
     "framework": "nextjs",
     "buildCommand": "next build",
     "installCommand": "npm ci",
     "outputDirectory": ".next"
   }
   ```

4. **Node.js Version:** set **22.x** under **Project Settings → General → Node.js Version** (matches
   `web/Dockerfile`, which builds on `node:22`). Vercel takes the Node version from Project Settings
   (or a `package.json` `engines.node`), not from `vercel.json` — this ticket leaves `package.json`
   untouched, so pick it in the UI.

## 2. Set Environment Variables

Add these under **Project Settings → Environment Variables**. Set them for **Production** and
**Preview** (and **Development** if you use `vercel dev`). Every default below is the app's built-in
fallback — the dashboard boots without any of them (it just renders the mock/`—` state, see §4).

**Backing-store config (server-side, never exposed to the browser):**

| Variable | Purpose | Example | Store as |
|---|---|---|---|
| `TALLY_CLICKHOUSE_URL` | ClickHouse HTTP(S) endpoint | `https://abc.clickhouse.cloud:8443` | Plain env |
| `TALLY_CLICKHOUSE_USER` | ClickHouse user | `tally` | Plain env |
| `TALLY_CLICKHOUSE_PASSWORD` | ClickHouse password | `••••••••` | **Encrypted / Sensitive** |
| `TALLY_CLICKHOUSE_DB` | ClickHouse database | `default` | Plain env |
| `TALLY_GATEWAY_URL` | Ingest gateway base URL | `https://gateway.example.com` | Plain env |
| `TALLY_TENANT_ID` | Tenant the dashboard reads | `local-dev` (prod: your tenant) | Plain env |

**Optional UI/build knobs (`NEXT_PUBLIC_*` are inlined at build time — non-secret by definition):**

| Variable | Purpose | Default |
|---|---|---|
| `NEXT_PUBLIC_TALLY_DASHBOARD_REFRESH_MS` | Live-poll cadence; `0` disables | `5000` |
| `NEXT_PUBLIC_API_BASE_URL` | Base URL for API calls made from the server during SSR | request-relative |

**Secrets handling:** put the ClickHouse password (and any other credential) in via Vercel's
encrypted env store and mark it **Sensitive** — Vercel encrypts it at rest and hides it from the UI
after save. **Never commit a secret**; `web/vercel.json` contains config only, no values. A changed
`NEXT_PUBLIC_*` value requires a **redeploy** to take effect (it is baked into the client bundle).

## 3. Deploy

- **Production:** push to `main` (or click **Deploy**). Vercel runs `npm ci` → `next build` in `web/`
  and serves the Route Handlers as Node.js serverless functions.
- **CLI (optional):**

  ```bash
  npm i -g vercel
  cd web
  vercel        # first run links the project (pick Root Directory web/ if prompted)
  vercel --prod # promote to production
  ```

## 4. Backing-store reachability + fail-soft behavior

**Reachability requirement.** Vercel serverless functions egress to the **public internet**, so the
backing stores must be reachable from Vercel:

- **ClickHouse:** use a **public HTTPS endpoint** (e.g. ClickHouse Cloud on `:8443`). A store that
  only listens on a private VPC/localhost (the docker-compose default `http://localhost:8123`) is
  **not** reachable from Vercel — you'd need ClickHouse Cloud, a public host with TLS + auth, or a
  tunnel (e.g. Cloudflare Tunnel / ngrok) / Vercel↔VPC peering. Same for `TALLY_GATEWAY_URL`.
- Provisioning those stores is **out of scope** (see the [GCP](../gcp/README.md) deploy).

**Fail-soft (no crash when unreachable).** The dashboard is designed to render cleanly even when the
stores can't be reached — this is intentional, not a bug:

- Every ClickHouse query goes through `tryLive()` in `web/lib/clickhouse.ts`, which catches any error
  (connection refused, timeout, DNS) and returns `null`. The Route Handlers then fall back to the
  typed mock in `web/lib/mock.ts` (`live ?? mock`), and honest-null fields render as `—` in the UI.
- Every gateway helper (reconciliation / integrations / guardrails / replay) does the same: a 2s
  timeout, non-2xx, or unreachable host returns `null`, and the route falls back to its static mock.

So a fresh Vercel deploy with **no env vars set** still boots and paints the whole dashboard against
mock data — nothing throws, no page 500s. As you wire real, reachable env values in §2, the pages
switch to live data. This is the same fail-soft path exercised by `web/`'s test suite and by a fresh
`npm run dev`, so "stores unreachable from Vercel" degrades to the exact mock/`—` state by design.

## 5. Preview deploys

Every pull request gets an automatic **Preview Deployment** at a unique URL. Previews use the
**Preview**-scoped Environment Variables from §2 — point them at a **staging** ClickHouse/gateway (or
leave them unset to preview against mock data). Never point Preview at production credentials.
`web/vercel.json` sets `git.deploymentEnabled.main = true`; PR previews remain on by default.

---

## Quick checklist

- [ ] Project imported, **Root Directory = `web/`**.
- [ ] Node.js Version = **22.x** (Project Settings).
- [ ] Env vars set for Production + Preview; ClickHouse password marked **Sensitive**.
- [ ] `TALLY_CLICKHOUSE_URL` / `TALLY_GATEWAY_URL` point at **publicly reachable** HTTPS endpoints
      (or left unset to run on mock data).
- [ ] Deploy is green; pages render (live where reachable, `—`/mock otherwise).
