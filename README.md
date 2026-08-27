# ai-tally

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

[![Watch the Demo Video](https://github.com/jain-aanchal/ai-tally/blob/main/Thumbnail.png)](https://github.com/jain-aanchal/ai-tally/releases/download/V1.0/AI-tally.Demo.mov)

Click on the thumbnail to watch the demo.

Cost-and-value observability for AI products. See what your AI actually costs (all-in) and what it returns.

Seven workflows on one shared data spine:

1. **Agent loop cost visibility.** Why did this run cost 50× median? Per-agent cost, run distribution (p50/p99), and the pathological runs that blow the budget, with a windowed daily-average cost per agent (CTO-226).
2. **Cross-provider comparison.** Are we on the right model? Real replay, real eval, no marketing benchmarks. Google/Gemini is a first-class priced provider here (CTO-149), not a mock column, and Compare ranks it against OpenAI and Anthropic on real cost.
3. **End-to-end cost.** What does this feature really cost? Every cost layer has a real ingest path: LLM tokens, tools, and embeddings from instrumented spans; vector from wrapped clients (Pinecone/Weaviate/Qdrant + Vertex Vector Search); and compute + egress from daily cloud-billing connectors (AWS Cost Explorer / GCP Billing / Vercel / Cloudflare). Compute and egress are configured per-tenant via the gateway API today; their `/connectors` tiles land with the per-tenant connector UI.
4. **Business-outcome attribution.** Is this AI feature profitable? `$/conversion` and margin per provider, joined on a hashed user id. The chatbot demo (`make chatbot-demo`) proves it end-to-end with synthetic conversions; production tenants wire their own revenue source via the gateway's Stripe webhook or the generic revenue API.
5. **Cost per customer.** Which customers cost you the most, and which pay for themselves? Direct AI spend attributed to each tenant customer by hashed account id, plus each account's allocated share of shared compute/egress (pro-rata on direct spend, the allocation rule named on screen), and gross margin per account once a revenue source is wired. Optional human-readable account labels, an account search, and a per-account detail view (CTO-176).
6. **Spend forecasting.** Where does this month land, and when do you cross budget? A day-of-week-weighted median projection of month-end AI spend with an 80% confidence cone and a breach date, per tenant and per scope (feature/model/layer), refusing to project below a 14-day settled-history floor rather than drawing a volatile number. Surfaced as the burn-down on `/cost` and a compact "Monthly predicted AI cost" card on Home (CTO-204/210/211/227).
7. **Waste detection.** Where are we paying for AI that returns nothing? Five detector categories surface recoverable spend: billed runs that failed, retried failed work, over-sized models, spend with no measured return, and structural inefficiency. Each finding names where the waste is, an estimated recoverable amount, a confidence, and a drill-through into the run/compare/attribution surface behind it. Honest under uncertainty: a finding that cannot defensibly bound its recoverable dollars renders a blank with a reason, never a fabricated or zero figure, and every finding is a hypothesis with evidence rather than a verdict (epic CTO-227).

An eighth surface (pre-deploy "what will this change cost?") is half-built. The infrastructure (replay sampling + per-candidate cost) ships today via Workflow 2; what's missing is a body-driven what-if form that accepts a candidate model + prompt override. Tracked separately, page hidden from the nav until it has signal end-to-end.

Behind the cost connectors, a per-tenant **scheduler** in the gateway (CTO-212) runs the recurring jobs on a cadence, with per-tenant locking so a slow run never overlaps itself: cloud cost connectors, reconciliation, and third-party ingest workers.

## Product principles

- **Honest under uncertainty.** Render `—` rather than fabricate a number. A quality cell with no eval pass behind it is `—`, not "85%". A p95 latency built from fewer than 50 spans is `—`, not noise. Misleadingly-rosy zeros are worse than empty space.
- **No bodies in telemetry.** Token counts and drop counts, never message text. The PII guard at the gateway suffix-matches keys like `prompt`, `messages`, `completion`, `body` and drops them on the floor. This is the contract, not a flag.
- **Billing decoupled from sampling.** A head-time meter counts every trace before the sampling decision, so invoices are exact regardless of analytics sample rate.
- **Tail-aware, not median-aware.** Agent cost is a power law; stratified sampling keeps the tail at ~100% and samples the cheap body down.
- **Never corrupt customer state.** Guardrails default to OBSERVE (record what would have fired), never hard-kill.
- **OTel-native.** Built on OpenTelemetry `gen_ai.*` conventions; extensions namespaced under the same.

## Dashboard

The Next.js dashboard is a grouped set of pages sharing one interactive foundation (CTO-220, "interactive design overhaul"), rebuilt from static server-rendered tables into a dynamic surface closer to what AWS / GCP / Datadog cost tools offer, without dropping a single feature or honest blank:

- **Overview:** Home, with the headline spend tiles, the ROI snapshot, per-provider conversion, and the "Monthly predicted AI cost" forecast card.
- **Analyze:** Cost (spend by layer + by feature, budget vs actual, burn-down), Features (per-feature economics), Agents (per-agent distribution and outlier runs), Compare (cross-provider replay + eval), Attribution (`$/conversion` and margin per provider), Unit Economics (CAC / LTV / payback), Cost per Customer (direct + allocated cost and margin per account), and Waste (recoverable spend across the five detector categories).
- **Configure:** Connectors (which cost layers and providers a tenant streams in), Guardrails (observe/enforce rules and audit), and Budgets (per-tenant and per-scope monthly budgets).

The shared foundation, merged before any page adopted it:

- **A global filter bar (URL-synced).** Time range (7 / 30 / 90 days / custom), a group-by dimension (feature / model / layer / provider / account), and multi-select dimension filters. State lives in the query string, so a filtered view is shareable and the browser back button walks the filter history. Windows are ClickHouse-clock derived, never the Node clock, now that they are user-selectable (CTO-203/226).
- **Interactive charts.** The inline-SVG charts gained hover tooltips, legend toggling, and click-to-drill (click a series to add it as a filter). No charting library: the charts stay inline SVG and theme-token driven.
- **Summary tiles and a refreshed shell.** Dense KPI tiles through the honest `Money` / `Pct` / `Blank` primitives, a grouped sticky sidebar with active-route highlighting, and a consistent page header / toolbar.
- **Live, honest, decoupled.** Each page server-renders once, then a visibility-aware `useLivePoll` refreshes it; a filter change re-fetches immediately rather than waiting for the next tick. Every value shown is a real measurement or a blank with a reason, never a fabricated or flattering zero.

## Waste detection

`/waste` (epic CTO-227) answers "where is this tenant paying for AI that returns nothing" with five detectors, each owning its own query and a pure, unit-tested detector:

- **Paid for nothing** (CTO-229): billed runs that ended failed or abandoned, so the tokens bought no result. The wasted spend is directly observed, so this is the highest-confidence finding and its recoverable is always bounded.
- **Duplicated work** (CTO-230): error-then-retry only. A run that failed and was superseded by a later same-shape success is spend the retry made redundant. A pure repeat with no failure is NOT claimed as waste: with no message bodies in telemetry it is indistinguishable from legitimate multi-turn use, so flagging it would fabricate savings. Medium confidence, and the reason says so.
- **Wrong-sized model** (CTO-231, per-call basis CTO-236): a cheaper candidate that ties on quality. The candidate's per-call cost comes from resolved-context replay; the incumbent's per-call cost is measured on its own real traffic; the two are compared per call and gated on the pairwise-judge eval CI. It needs a captured replay corpus plus a judged eval pass to fire, otherwise it stays honestly blank (see CTO-236 / CTO-237). There is no mock path.
- **No measured return** (CTO-232): spend on a feature with zero attributed value, but only on a tenant that attributes value elsewhere. It is a flag to investigate, not a verdict (top-of-funnel work or revenue simply not yet wired looks identical from telemetry alone), and it returns nothing when the whole tenant has no revenue source wired.
- **Structural inefficiency** (CTO-233): context bloat and runaway agent loops, judged against each feature/agent's own median with robust stats (Tukey IQR fence plus a relative floor), never a global average, so a uniformly heavy feature produces no outliers.

Shared rules across all five: findings are hypotheses with evidence, not verdicts; money is integer micro-USD, and a recoverable that cannot be defensibly bounded renders a blank with a reason, never `0`; windows are ClickHouse-clock derived. `/waste` lives on the same interactive foundation as the rest of the dashboard (filter bar, live re-query) and the whole thing reads `otel_spans`, the attribution join, and the Compare replay/eval results. On clean data most detectors correctly find little; the value is that the numbers shown are honest.

## Repository layout

```
sdk/python/        Python SDK (OTel gen_ai.* + cost/feature/identity/sampling/guardrails)
infra/gateway/     Ingest gateway (FastAPI: auth → enrich cost → ClickHouse),
                   plus per-tenant control plane (connectors, replay, eval, guardrails, CAC)
infra/edge-proxy/  Zero-code edge proxy (Go) + BYO-deployment Helm chart
infra/             docker-compose stack (ClickHouse, Postgres, Redpanda, MinIO) + Makefile
db/clickhouse/     ClickHouse DDL: otel_spans, attribution, business_events, replay_samples, eval_runs
db/postgres/       Postgres control-plane schema: tenants, connectors, stripe, replay,
                   eval, guardrails, CAC, integration runs
web/               Next.js dashboard: all workflows on one interactive foundation
                   (global filter bar, live interactive charts, drill-downs)
examples/          End-to-end demos: Aider edge-proxy traffic, Vercel AI Chatbot
```

## Running it

To bring up the whole stack on a laptop and see ingested telemetry in the dashboard, follow **[RUNNING.md](./RUNNING.md)**, a verified end-to-end runbook. Short version:

```bash
cd infra && make up && make seed && make demo   # stack + tenant + sample telemetry
cd web && npm install && npm run dev            # dashboard at http://localhost:3000
```

The runbook covers nine end-to-end steps, including the demos that exercise each workflow.

### Demos by workflow

| Workflow | Command | What it does |
|---|---|---|
| Agent loop + edge proxy | `make aider-demo` | Drives Aider against a fixture repo through the edge proxy (`PROVIDER=anthropic`/`google` for cross-provider variants; Gemini runs direct, see `examples/aider-demo/README.md`) |
| Business-outcome attribution | `make chatbot-demo` | 50 scripted chat sessions across OpenAI + Anthropic with thumbs-up conversion events |
| Real revenue via Stripe | RUNNING.md §7 | Verified webhook ingest → `business_events` → `$/conversion` |
| Replay-backed Compare | RUNNING.md §8 | Opt-in 5% sampling, cross-provider replay with daily budget cap |
| Pairwise LLM-judge quality | RUNNING.md §9 | Pairwise judge with Wilson 95% CIs on win-rate |

Demos need provider keys exported: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. `GOOGLE_API_KEY` / `GEMINI_API_KEY` is optional: the demos run without it, and enable the Google/Gemini models (chatbot picker + `PROVIDER=google make aider-demo`) when present.

Deploying on GCP? See `deploy/gcp/` (CTO-153).

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

On startup the gateway hits `GET /v1/models` on every provider whose API key it has (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and, since CTO-149, `GOOGLE_API_KEY` / `GEMINI_API_KEY` for Google's model list), classifies each id into a coarse family (`haiku` / `sonnet` / `opus` / `mini` / `flagship` / `flash` / `embedding`), and writes the result to `.tally/models.json` with a 24h TTL. The demos read that file via `tally.models.latest_anthropic("sonnet")` (Python) or `resolveLatest()` (Node), so when a provider retires a SKU (`claude-3-5-haiku-latest` was the case that prompted this) the next boot picks up the replacement automatically.

Knobs:

- `TALLY_MODELS_REFRESH=1`: bypass the 24h TTL and refetch on the next boot.
- `TALLY_PINNED_MODELS=<path>`: skip discovery entirely, load the lineup from that file (useful for CI runs that must be hermetic).
- `TALLY_MODELS_CACHE=<path>`: Node-side override for where `resolveLatest()` reads the cache from.

Discovery is fail-soft: if all providers are unreachable and the cache file doesn't exist, the gateway boots with an empty list and a warning, and consumers fall back to pinned defaults.

Both the demo and the dashboard now consume that cache rather than shipping frozen SKUs. The chatbot demo resolves each picker slot (OpenAI / Anthropic / Google `flash`+`pro`) from the discovered lineup at launch, falling back to a pinned id per slot when the cache is missing, so a retired SKU self-heals on the next boot with no code change. The dashboard's Compare candidate list is validated against the current catalog by a guard test, so a retired id can't silently persist.

## Replay-backed Compare

Workflow 2 (Compare) used to be a mock projection rescaled off the user's real current-model spend. It's now backed by **real cross-provider replay**: the gateway captures an opt-in 5% sample of spans, scrubs PII (emails, API keys, postal addresses), stores the resolved request envelope in object storage, and replays it against candidate models on demand.

Per-tenant opt-in: default off; nothing is sampled until a tenant flips `enabled=true` via `POST /v1/tenant/replay/config`. A daily budget cap (default `$5/day`) hard-stops the replay executor from running away. The diagnostics block on `/api/compare` carries the honest fidelity string `"resolved-context replay (no live retrieval)"` so the dashboard never claims a tier it doesn't have.

When a tenant has no opted-in samples (or the gateway is unreachable), `/api/compare` falls back to the rescaled-mock path it had before. `replay_source` in the response distinguishes the two branches.

## Pairwise LLM-judge eval

The Quality column on `/compare` is grounded in a real eval pass: pairwise LLM-judge over replay outputs, with A/B order randomized per sample to mitigate position bias, win-rate scored as a Wilson 95% CI. Opt-in separately from replay (judge calls run a frontier model and are pricier than candidate replays); default daily budget `$10/day`, default judge `claude-opus-4-8`, rubric tagged `rubric-v1` so a future tightening stays interpretable.

Below the 10-judged-samples floor, the cell renders `—` with the hint *"needs ≥10 judged samples, run eval pass"*. There is **no fallback to mock here, by design**: a fake quality number is worse than no quality number.

## Stripe → real revenue (production)

> **Status (what works, what doesn't, as of today):**
>
> | Layer | State |
> |---|---|
> | Webhook ingest `POST /v1/stripe/webhook` (signature-verified) | ✅ ships |
> | Control-plane config `POST /v1/tenant/stripe/config` (per-tenant signing secret) | ✅ ships |
> | Stripe → `business_events` event mapping (`checkout.session.completed` → conversion, `invoice.paid` → renewal, `charge.refunded` → negative) | ✅ ships |
> | HMAC join: Stripe `customer.email` → SDK's `UserIdHash` space | ✅ ships |
> | `/attribution` Value/user + Margin/user columns | ✅ ships (cells stay `—` until enough events arrive) |
> | Dashboard tile to paste a signing secret (the "Connect Stripe" affordance) | ⚠️ **removed in [#102](https://github.com/jain-aanchal/ai-tally/pull/102), not yet replaced**; wire-up is currently API-only |
> | Backfill helper `scripts/backfill_stripe.py --days N` | ✅ ships |
>
> Net: the **ingest backend is fully wired**; the **dashboard wire-up affordance is hidden** until the per-tenant connector UI ships.

The chatbot demo (`make chatbot-demo`) exercises the attribution join with synthetic `positive_feedback` events. No Stripe wire-up is needed to see `$/conversion` light up locally. For production tenants, Stripe is the v1 revenue source, but the setup path today is the gateway API, not the dashboard.

To wire it from outside the dashboard:

```bash
# production: POST your Stripe signing secret to the gateway
curl -X POST http://<gateway>/v1/tenant/stripe/config \
  -H "x-tenant-id: <tenant-uuid>" -H "content-type: application/json" \
  -d '{"webhook_secret": "whsec_..."}'

# local dev: stripe-cli forwards directly to the gateway
stripe listen --forward-to http://localhost:8080/v1/stripe/webhook?tenant=local-dev
```

Once events start landing, `/attribution` adds two columns: **Value/user** and **Margin/user** (with margin %). Cells stay `—` until enough events arrive. We never fabricate numbers from absent data.

### Not on Stripe?

`POST /v1/revenue/events` takes `{event_id, account_id, amount, currency, occurred_at, event_name}`
from any biller: Chargebee, Recurly, Zuora, or a home-grown one. It is idempotent on your own
`event_id`, so a retry cannot double-count, and it writes the same `business_events` rows the
connectors do. See [`docs/revenue-api.md`](docs/revenue-api.md).

## Per-tenant control plane

Stored in Postgres (`db/postgres/*.sql`, numbered migrations `0001` upward), accessed only through the gateway (the web app never talks to Postgres directly):

- **Tenants + API keys + HMAC key versions** for per-tenant user-id hashing
- **Cost-layer connector declarations** (which of LLM / vector / tools / compute / egress this tenant streams in)
- **Compute + egress connector config** (`tenant_compute_config`, `tenant_egress_config`): cloud provider, a credentials *reference* (Secret Manager / KMS, never a raw key), and `last_run_at` / `last_status`
- **Stripe config**, **replay config**, **eval config**, **guardrail rules** + audit log, **BigQuery export config**
- **CAC periods** for the unit-economics workflow (one row per finance-entered month)
- **Account labels + cost-allocation config** for the cost-per-customer workflow (optional human-readable name per hashed account id; the allocation rule used to split shared compute/egress across accounts)
- **Per-tenant + per-scope monthly budgets** the forecast measures against (`tenant_budgets`)
- **Scheduler run bookkeeping** (`scheduler_runs`) and **third-party ingest cursors** (`tenant_ingest_cursors`) for the scheduler and its workers
- **Integration run status** for third-party connectors (workers call `record_run` after each cycle with `last_run_at` + 24h/7d event counts; the surfacing UI card was removed pending the real per-connector UI)

Every control-plane write is audited with an idempotent `change_id` (UUID), and `INSERT … ON CONFLICT DO NOTHING` makes a UI double-click safe.

## Status

The shipped workflows are wired end-to-end on a laptop with `make chatbot-demo`, and the dashboard is now interactive across every page (filter bar, live charts, drill-downs) rather than a set of static tables. Each `—` you see on a dashboard tile is honest, a placeholder for a metric we haven't grounded yet. Every `/cost` column has a real ingest path (LLM / tools / embeddings / vector from spans, compute / egress from cloud-billing connectors), and the cost-per-customer and forecasting workflows read the same spine. Waste detection shipped too: the five detectors and the `/waste` page are live, with the wrong-sized-model detector functional on a per-call basis (CTO-236) once replay capture is wired into ingest (CTO-237); it fires only with a captured replay corpus plus a judged eval pass, and stays blank otherwise. The remaining backlog turns the last `—`s into real numbers (guardrail trip counts, body-driven pre-deploy estimation) and broadens provider/cloud coverage (Amazon Bedrock, the Vercel AI Gateway, AWS/Vercel deploys).

Decisions and the full system spec live in the project tracker. Tickets follow a Context / Acceptance criteria / Out-of-scope format and are picked up one PR at a time.

## License

ai-tally is licensed under the [Apache License, Version 2.0](LICENSE). Required attribution notices for the project and any third-party dependencies live in [NOTICE](NOTICE).

We follow the [Contributor Covenant](CODE_OF_CONDUCT.md) Code of Conduct.
