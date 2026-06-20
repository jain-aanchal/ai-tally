# ai-tally

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Cost-and-value observability for AI products. See what your AI actually costs — all-in — and what it returns.

Five workflows on one shared data spine:

1. **Agent loop cost visibility** — why did this run cost 50× median?
2. **Cross-provider comparison** — are we on the right model? Real replay, real eval, no marketing benchmarks.
3. **End-to-end cost** — what does this feature really cost, all-in (LLM + vector + tools + compute + egress)?
4. **Business-outcome attribution** — is this AI feature profitable? `$/conversion` and margin per provider, joined from real Stripe revenue.
5. **Pre-deploy estimation** — what will this change cost before we ship?

## Product principles

- **Honest under uncertainty** — render `—` rather than fabricate a number. A quality cell with no eval pass behind it is `—`, not "85%". A p95 latency built from fewer than 50 spans is `—`, not noise. Misleadingly-rosy zeros are worse than empty space.
- **No bodies in telemetry** — token counts and drop counts, never message text. The PII guard at the gateway suffix-matches keys like `prompt`, `messages`, `completion`, `body` and drops them on the floor. This is the contract, not a flag.
- **Billing decoupled from sampling** — head-time meter counts every trace before the sampling decision, so invoices are exact regardless of analytics sample rate.
- **Tail-aware, not median-aware** — agent cost is a power law; stratified sampling keeps the tail at ~100% and samples the cheap body down.
- **Never corrupt customer state** — guardrails default to OBSERVE (record what would have fired), never hard-kill.
- **OTel-native** — built on OpenTelemetry `gen_ai.*` conventions; extensions namespaced under the same.

## Repository layout

```
sdk/python/        Python SDK (OTel gen_ai.* + cost/feature/identity/sampling/guardrails)
infra/gateway/     Ingest gateway (FastAPI: auth → enrich cost → ClickHouse)
                   plus per-tenant control plane (connectors, replay, eval, guardrails, CAC)
infra/edge-proxy/  Zero-code edge proxy (Go) + BYO-deployment Helm chart
infra/             docker-compose stack (ClickHouse, Postgres, Redpanda, MinIO) + Makefile
db/clickhouse/     ClickHouse DDL — otel_spans, attribution, business_events, replay_samples, eval_runs
db/postgres/       Postgres control-plane schema — tenants, connectors, stripe, replay,
                   eval, guardrails, CAC, integration runs
web/               Next.js dashboard (the five workflows)
examples/          End-to-end demos: Aider edge-proxy traffic, Vercel AI Chatbot, Stripe
```

## Running it

To bring up the whole stack on a laptop and see ingested telemetry in the dashboard, follow
**[RUNNING.md](./RUNNING.md)** — a verified end-to-end runbook. Short version:

```bash
cd infra && make up && make seed && make demo   # stack + tenant + sample telemetry
cd web && npm install && npm run dev            # dashboard at http://localhost:3000
```

The runbook covers nine end-to-end steps, including the demos that exercise each workflow.

### Demos by workflow

| Workflow | Command | What it does |
|---|---|---|
| Agent loop + edge proxy | `make aider-demo` | Drives Aider against a fixture repo through the edge proxy |
| Business-outcome attribution | `make chatbot-demo` | 50 scripted chat sessions across OpenAI + Anthropic with thumbs-up conversion events |
| Real revenue via Stripe | RUNNING.md §7 | Verified webhook ingest → `business_events` → `$/conversion` |
| Replay-backed Compare/Estimate | RUNNING.md §8 | Opt-in 5% sampling, cross-provider replay with daily budget cap |
| Pairwise LLM-judge quality | RUNNING.md §9 | Pairwise judge with Wilson 95% CIs on win-rate |

Demos need provider keys exported: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.

## Development

The Python SDK uses [uv](https://docs.astral.sh/uv/), `ruff`, and `pytest`.

```bash
cd sdk/python
uv sync
uv run ruff check .
uv run pytest
```

Gateway tests:

```bash
cd infra/gateway && uv run pytest
```

Web tests:

```bash
cd web && npx vitest run
```

## Model auto-discovery

On startup the gateway hits `GET /v1/models` on every provider whose API key it has (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`), classifies each id into a coarse family (`haiku` / `sonnet` / `opus` / `mini` / `flagship` / `embedding`), and writes the result to `.tally/models.json` with a 24h TTL. The demos read that file via `tally.models.latest_anthropic("sonnet")` (Python) or `resolveLatest()` (Node), so when a provider retires a SKU — `claude-3-5-haiku-latest` was the case that prompted this — the next boot picks up the replacement automatically.

Knobs:

- `TALLY_MODELS_REFRESH=1` — bypass the 24h TTL and refetch on the next boot.
- `TALLY_PINNED_MODELS=<path>` — skip discovery entirely, load the lineup from that file (useful for CI runs that must be hermetic).
- `TALLY_MODELS_CACHE=<path>` — Node-side override for where `resolveLatest()` reads the cache from.

Discovery is fail-soft: if both providers are unreachable and the cache file doesn't exist, the gateway boots with an empty list and a warning. The demos fall back to their hardcoded defaults.

## Replay-backed Compare and Estimate

Workflows 2 (Compare) and 5 (Estimate) used to be mock projections rescaled off the user's real current-model spend. They're now backed by **real cross-provider replay**: the gateway captures an opt-in 5% sample of spans, scrubs PII (emails, API keys, postal addresses), stores the resolved request envelope in object storage, and replays it against candidate models on demand.

Per-tenant opt-in — default off; nothing is sampled until a tenant flips `enabled=true` via `POST /v1/tenant/replay/config`. A daily budget cap (default `$5/day`) hard-stops the replay executor from running away. The diagnostics block on `/api/compare` carries the honest fidelity string `"resolved-context replay (no live retrieval)"` so the dashboard never claims a tier it doesn't have.

When a tenant has no opted-in samples (or the gateway is unreachable), the `/api/compare` and `/api/estimate` routes fall back to the rescaled-mock path they had before — `replay_source` in each response distinguishes the two branches.

## Pairwise LLM-judge eval

The Quality column on `/compare` is grounded in a real eval pass — pairwise LLM-judge over replay outputs, with A/B order randomized per sample to mitigate position bias, win-rate scored as a Wilson 95% CI. Opt-in separately from replay (judge calls run a frontier model and are pricier than candidate replays); default daily budget `$10/day`, default judge `claude-opus-4-8`, rubric tagged `rubric-v1` so a future tightening stays interpretable.

Below the 10-judged-samples floor, the cell renders `—` with the hint *"needs ≥10 judged samples — run eval pass"*. There is **no fallback to mock here, by design** — a fake quality number is worse than no quality number.

## Stripe → real revenue

Connect Stripe via the `/connectors` UI (paste a signing secret) or `stripe listen` for local dev, and the gateway's verified webhook handler maps Stripe events to `business_events` rows — `checkout.session.completed` → conversion, `invoice.paid` → renewal, `charge.refunded` → negative revenue. Stripe customer emails are HMAC-hashed into the same `UserIdHash` space the SDK uses, so the attribution join lights up the moment events land.

Two new columns appear on `/attribution` once a tenant wires Stripe: **Value/user** and **Margin/user** (with margin %). Cells stay `—` until enough events arrive — we never fabricate numbers from absent data.

## Per-tenant control plane

Stored in Postgres (`db/postgres/000{1..7}_*.sql`), accessed only through the gateway (the web app never talks to Postgres directly):

- **Tenants + API keys + HMAC key versions** for per-tenant user-id hashing
- **Cost-layer connector declarations** (which of LLM / vector / tools / compute / egress this tenant streams in)
- **Stripe config**, **replay config**, **eval config**, **guardrail rules** + audit log
- **CAC periods** for the unit-economics workflow (one row per finance-entered month)
- **Integration run status** for third-party connectors (light up the connector card with real `last_run_at` and 24h/7d event counts)

Every control-plane write is audited with an idempotent `change_id` (UUID), and `INSERT … ON CONFLICT DO NOTHING` makes a UI double-click safe.

## Status

The five workflows are wired end-to-end on a laptop with `make chatbot-demo`. Each `—` you see on a dashboard tile is honest — a placeholder for a metric we haven't grounded yet. The remaining backlog turns those `—`s into real numbers (per-feature attribution, candidate-response replay for honest eval grading, real workers for the "Coming soon" connectors).

Decisions and the full system spec live in the project tracker. Tickets follow a Context / Acceptance criteria / Out-of-scope format and are picked up one PR at a time.

## License

ai-tally is licensed under the [Apache License, Version 2.0](LICENSE). Required attribution notices for the project and any third-party dependencies live in [NOTICE](NOTICE).

We follow the [Contributor Covenant](CODE_OF_CONDUCT.md) Code of Conduct.
