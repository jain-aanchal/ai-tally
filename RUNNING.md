# Running ai-tally locally (end-to-end)

This walks the full pipeline on a laptop: **send telemetry → ingest gateway → ClickHouse →
dashboard**. Every step below has been run and verified; the dashboard renders the spans you
ingest, not mock data.

```
 send_batch.py / curl ──POST /v1/batches──▶  gateway (:8080)
                                                 │  auth → rate-limit → idempotency →
                                                 │  validate → enrich cost → map to row
                                                 ▼
                                            ClickHouse  otel_spans
                                            (:8123, db=default, TenantId=<tenant UUID>)
                                                 ▲
   browser ──▶ Next.js web (:3000) ──Route Handler──┘  (web/lib/clickhouse.ts, TALLY_DEV_TENANT)
```

Everything is keyed to the tenant `make seed` creates, and the key is its **UUID**, not the name
`local-dev` (Initiative 1 §8). Ingest writes `TenantId` verbatim from the batch and the dashboard
binds `TALLY_DEV_TENANT` straight into the read filter (`TenantId = ...`), so the two only line up
when both carry the UUID. Send a batch under the name and it lands in ClickHouse and never appears
on screen: no error anywhere, just an empty dashboard. `make seed` prints the UUID; `make demo`
resolves it for you and refuses to send without it.

(Control-plane calls are the exception: `/v1/tenant/*` accepts either, because the gateway folds a
name onto the UUID. Reads do not.)

## Prerequisites

- Docker (Compose v2): `docker version` should print a server version.
- Node.js + npm (for the web app).
- [uv](https://docs.astral.sh/uv/) (only if you want to run the gateway or its tests outside Docker).

---

## 1. Bring up the backing stack + gateway

```bash
cd infra
make up
```

This starts ClickHouse (`8123`), Postgres (`5432`), Redpanda (`9092`), MinIO (console `9001`), and
**builds + runs the gateway container on host port `8080`**. The canonical DDL in `db/` is applied
to ClickHouse on first boot. Wait ~20s, then confirm:

```bash
make ps                            # all services "healthy"
curl -s localhost:8080/healthz     # {"status":"ok"}
```

> The gateway waits for ClickHouse + Postgres to pass health checks before it boots, so a brief
> "starting" is normal.

> On first boot the gateway also hits `GET /v1/models` on every provider whose API key it has and
> writes the result to `.tally/models.json` (CTO-109). Subsequent boots reuse that file for 24 h;
> set `TALLY_MODELS_REFRESH=1` to force a refetch, or `TALLY_PINNED_MODELS=<path>` to skip discovery
> entirely. If both providers are unreachable, boot still succeeds; you'll just see a warning in
> the gateway log and the demos fall back to their hardcoded model defaults.

The composed gateway already sets `TALLY_CLICKHOUSE_DB=default` (the official ClickHouse image loads
unqualified DDL into the `default` database; see the note in `infra/docker-compose.yml`). Running
the gateway *by hand* with the library default `TALLY_CLICKHOUSE_DB=tally` will fail with
`Database tally does not exist`; pass `TALLY_CLICKHOUSE_DB=default` if you do.

## 2. Seed the local tenant

```bash
make seed     # creates the `local-dev` tenant + API key + feature tags in Postgres
```

This prints a one-time API key (`tally_sk_…`). Only its SHA-256 is stored, so copy it if you plan to
enable auth. For local testing auth is **off** by default (`TALLY_REQUIRE_API_KEY=false`).

### Control-plane auth when you flip `TALLY_REQUIRE_API_KEY=true`

The `/v1/tenant/*` control plane is authenticated separately from ingest (Initiative 1 §6). With auth
on, every control-plane call needs a bearer **service token** (`TALLY_GATEWAY_SERVICE_TOKEN` on the
gateway, `GATEWAY_SERVICE_TOKEN` on the web server, same value) plus an `x-tenant-id` header naming
the tenant. An ingest API key does **not** open the control plane, and the gateway refuses to boot
when auth is on and no service token is set, rather than coming up with an open control plane. So the
`curl … -H 'x-tenant-id: …'` examples below work as written only while auth is off; with auth on, add
`-H "Authorization: Bearer $TALLY_GATEWAY_SERVICE_TOKEN"`.

Ingest (`/v1/batches`) is unchanged: it still authenticates with the ingest API key above.

## 3. Push telemetry through the gateway

Easiest: the built-in demo batch (resolves the seeded tenant's UUID and writes under it, then
re-sends once to demonstrate idempotent replay; see [Batch idempotency](#batch-idempotency-cto-245) for exactly
what that guarantee covers and what it does not):

```bash
make demo            # run it a few times for more rows
```

Or fire your own burst. `tenant_id` **must** be the tenant UUID for the UI to show it, so grab it
first (this is the same lookup `make demo` and the demo-deploy kit do):

```bash
TENANT=$(docker compose exec -T postgres \
  psql -U tally -d tally -tAc "SELECT id FROM tenants WHERE name='local-dev' LIMIT 1" | tr -d '[:space:]')
test -n "$TENANT" || echo "not seeded, run 'make seed' first"

for i in $(seq 1 40); do
  curl -s -X POST localhost:8080/v1/batches \
    -H 'content-type: application/json' \
    -d '{"tenant_id":"'"$TENANT"'","sdk_version":"test","resource_spans":[
          {"trace_id":"tr'$i'","span_id":"s'$i'","gen_ai.system":"openai",
           "gen_ai.operation.name":"chat","gen_ai.request.model":"gpt-4o",
           "gen_ai.usage.input_tokens":1200,"gen_ai.usage.output_tokens":350}]}' >/dev/null
done
```

Verify the rows landed (and carry enriched cost):

```bash
curl -s 'http://localhost:8123/?user=tally&password=tally&database=default' \
  --data "SELECT count(), round(sum(EstimatedCost),4) FROM otel_spans WHERE TenantId='$TENANT'"
```

or open a SQL shell with `make ch` and run
`SELECT TenantId, FeatureTag, count(), sum(EstimatedCost) FROM otel_spans GROUP BY TenantId, FeatureTag`.
A `TenantId` that reads `local-dev` rather than a UUID is a batch that the dashboard will not
render: re-send it under the UUID.

### Nullable usage and cost (CTO-244)

`InputTokens`, `OutputTokens`, `CachedInputTokens` and `EstimatedCost` are **nullable**. "We do not
know" is a real state and it is not zero. The common case is a streamed response through the edge
proxy: usage cannot be scanned off the stream, so the provider never tells us what the call
consumed. Those spans store `NULL`, and `CostSource = 'unpriced'` records why the cost is missing
(the same empty `PriceCatalogVersion` that `tally.pricing` already returns on a catalog miss). A
cost or token count that the provider really did report as `0` still stores as `0` and stays
distinguishable from `NULL`.

This matters when you read the data yourself. `sum()` skips NULLs, so a plain
`sum(EstimatedCost)` is a **lower bound**, not a total. Ask for the coverage alongside it:

```sql
SELECT sum(EstimatedCost)                AS known_spend,
       countIf(EstimatedCost IS NULL)    AS unpriced_spans,
       count()                           AS spans
FROM otel_spans WHERE TenantId = 'local-dev';
```

The rollups carry the same disclosure as `UnpricedSpanCount` and `UnknownUsageSpanCount` columns.
Anything that divides cost by a count (per call, per user, per conversion, per token) is a ratio
between a partial numerator and a complete denominator whenever `unpriced_spans > 0`; the dashboard
renders those as a blank with a reason rather than a smaller number.

**Applying it to a stack that is already up.** Run `make ch-migrate` from `infra/`. Unlike the
earlier additive column migrations, this one issues `MODIFY COLUMN`, which is a real ClickHouse
mutation: it rewrites the affected parts in the background (watch `system.mutations`) instead of
being metadata-only. Ingest keeps working while it runs. Replaying is safe, because modifying a
column to the type it already has is a no-op.

**Known limitation: figures from before this cutover may understate spend.** Rows written earlier
hold `0` where the truth was either a genuine provider-reported zero **or** an unknown that ingest
flattened to zero. Nothing recorded which, so nothing can separate them now. There is deliberately
no backfill: guessing which zeros "should" be NULL would fabricate exactly the kind of number this
change removes. So pre-cutover totals may be lower than the real spend, by an amount no query can
measure, and the pre-cutover `UnpricedSpanCount` of `0` means "nobody counted", not "there were no
unknowns". Post-cutover data is honest. If a clean baseline matters more than history on a local
stack, `make nuke && make up && make seed` and re-backfill.

### Batch idempotency (CTO-245)

Re-posting a batch with a `batch_id` the gateway has already accepted returns the original response
(`"replayed": true`) and writes nothing. That guarantee is now **durable**: the record lives in
Postgres (`ingest_batch_idempotency`, migration 0031), so it survives a gateway restart, a deploy,
a crash and a scale-out onto a second worker.

It did not before, and the consequence was real. The record used to live only in a dict inside one
gateway process, so a client that retried a batch across a restart was accepted a second time and
its spans were written again. `otel_spans` was a plain `MergeTree` with no deduplication, so the
second copy stayed forever and inflated every cost total by exactly the replayed spend. One local
end-to-end run that re-posted across two restarts left 333,689 rows holding 271,571 distinct
`SpanId`s: about 62,000 spans of permanently double-counted money.

**Check which mode your gateway is in.** It says so at boot:

```bash
make logs | grep -i "batch idempotency"
```

`durable batch idempotency enabled` is the fixed state. A `WARNING` about it being unavailable means
migration 0031 has not been applied (see below) and the gateway is running with the old, restart-
unsafe in-process cache. Set `TALLY_IDEMPOTENCY_DURABLE_REQUIRED=true` to make that a startup
failure instead of a warning; any deployment that depends on the guarantee should.

**If the store cannot be reached mid-flight, the gateway refuses the batch** with HTTP 503 and
`error.code = IDEMPOTENCY_UNAVAILABLE`, and the client retries. It does not accept on a failed
check. Accepting would re-admit the duplicate silently, and nothing in the data afterwards says
which dollars were counted twice; refusing is visible and costs only a delay.

**Applying it to a stack that is already up.** `docker-entrypoint-initdb.d` only fires on a first
boot against an empty volume, so an existing stack needs the migration by hand:

```bash
make psql < ../db/postgres/0032_ingest_batch_idempotency.sql   # from infra/
```

Then restart the gateway and confirm the boot line above.

**What is still exposed, stated plainly.**

* `otel_spans` is now `ReplacingMergeTree` with `TraceId, SpanId` appended to its sorting key, as a
  backstop for anything that gets past the idempotency check. ReplacingMergeTree collapses rows only
  when parts **merge**, so between a duplicate insert and that merge a plain `SELECT sum(...)` still
  sees both rows. Add `FINAL` when you need exactness now:
  `SELECT sum(EstimatedCost) FROM otel_spans FINAL WHERE TenantId = 'local-dev'`. **No read path in
  the dashboard was converted to `FINAL` in this change**, so dashboard totals converge on the merge
  rather than being exact the instant a duplicate lands. They are never worse than before, where the
  duplicate was permanent.
* **The rollups are not covered at all.** `daily_feature_rollup`, `hourly_feature_rollup` and
  `daily_account_rollup` are fed by materialized views that fire on INSERT into `otel_spans`, so a
  duplicate is summed into them before the engine ever sees it and no later merge removes it. A
  duplicate that reaches ClickHouse is permanent in the rollups. This is why the durable idempotency
  store is the real fix and the engine is only a backstop.
* A replay only collapses if the row is identical. An ordinary replay reproduces the same
  `Timestamp` (it comes from the client's span timestamp), but a span with no client timestamp, or
  one whose clock-skew assessment clamps against server receive time, can land on a different
  `Timestamp` on the replay and will not collapse.

**Existing ClickHouse installs are still on the old engine.** ClickHouse cannot `ALTER` a table's
engine or its `ORDER BY`, so `db/clickhouse/otel_spans.sql` gives a **fresh** database the right
engine and leaves an existing one exactly as it was; `make ch-migrate` cannot change it either.
Check what you actually have:

```sql
SELECT engine, sorting_key FROM system.tables
 WHERE database = currentDatabase() AND name = 'otel_spans';
```

If that is not `ReplacingMergeTree` with a sorting key ending in `TraceId, SpanId`, run the one-shot
migration, which creates the correctly-shaped table, copies the data, swaps the names atomically,
collapses the historical duplicates and restores the TTL:

```bash
make ch-migrate-otel-engine   # from infra/
```

Read the header of `db/clickhouse/migrations/otel_spans_replacing_engine.sql` first. It is a full
copy of the raw span table: it needs disk for a second copy, it takes as long as your table is
large, and rows written into the old table while the copy runs are not carried over, so quiesce
ingest for the duration. It keeps the pre-migration table as `otel_spans_cto245` for rollback.

## 4. Run the web dashboard

In a separate terminal:

```bash
cd web
npm install         # first run only
npm run dev
```

Open **http://localhost:3000**.

The ClickHouse connection needs no config: `web/lib/clickhouse.ts` defaults to exactly what the
stack uses: `http://localhost:8123`, database `default`. The **tenant** does need one variable.
There is no pinned default any more: on the product path the dashboard resolves the caller's Clerk
organization, so to run locally with no Clerk account set the dev escape hatch to the UUID that
`make seed` printed (Initiative 1 §10):

```bash
export TALLY_DEV_TENANT=<the UUID make seed printed>
npm run dev
```

Use the UUID, not `local-dev`: the value is bound into the ClickHouse read filter and a name matches
no rows. Leave it unset and the dashboard errors rather than guessing a tenant.

Each Route Handler queries ClickHouse live and falls back to mock data **only** if ClickHouse is
unreachable. With the stack up and a batch sent, the **Cost**, **Features**, **Agents**, and **Data
Quality** pages render your ingested spans.

---

## Optional: exercise the async ingest buffer (CTO-37)

The composed gateway uses the synchronous write path by default. To run the burst buffer that
decouples the request edge from ClickHouse (accept + ack immediately, drain in the background, never
5xx on a slow/down store), set it on the gateway service in `infra/docker-compose.yml`:

```yaml
  gateway:
    environment:
      TALLY_INGEST_BUFFERED: "true"
```

then `make up` again. You can watch the guarantee directly: stop ClickHouse, fire a burst, and POSTs
still return `200 accepted` while the drain loop logs `drain failed; retrying` and holds the rows
until ClickHouse returns. Knobs (all `TALLY_`-prefixed): `INGEST_BUFFER_CAPACITY` (default `200000`;
rows past this are shed as *retryable*, never 5xx), `INGEST_BUFFER_DRAIN_BATCH` (`2000`),
`INGEST_BUFFER_POLL_INTERVAL_S` (`0.05`).

## Step 5: real traffic via Aider

`make demo` posts a hand-crafted batch. To see ai-tally with **real agent
traffic** (same path a customer's app would take), run the Aider fixture
demo:

```bash
export OPENAI_API_KEY=sk-...   # or ANTHROPIC_API_KEY + PROVIDER=anthropic
cd infra && make aider-demo
```

Aider edits a small Python fixture across three multi-turn tasks. All LLM
requests transit the ai-tally edge proxy with `X-Tally-Feature-Tag:
aider-demo`, and the dashboard auto-opens to `/agents?tag=aider-demo` showing
the just-recorded runs.

Full walkthrough (including the cross-provider variant, what each task does,
and the architecture diagram) is in
[examples/aider-demo/README.md](examples/aider-demo/README.md).

When you're done: `make aider-demo-stop` kills the background proxy.

## Step 6: chatbot + conversion attribution

Aider is the right shape for agent-loop and cross-provider visibility (steps
1–3 of the five workflows). For **workflow 4: business-outcome
attribution**, run the chatbot demo. It vendors the Vercel AI SDK chatbot
template, drives 50 synthetic sessions split across OpenAI and Anthropic,
and emits conversion events (thumbs-up + session-engaged) so the
`/attribution` view can show $/conversion per provider.

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
cd infra && make chatbot-demo
```

`run.sh` (re)installs the chatbot's deps whenever `node_modules` is missing **or
stale**, i.e. `pnpm-lock.yaml` / `package.json` is newer than `node_modules`
(CTO-174), so a dependency bump is picked up automatically instead of failing
the build with `Module not found`. If a run ever trips over a stale install,
recover manually with `cd examples/vercel-chatbot/app && pnpm install`.

On launch the demo **refreshes the model lineup** (CTO-147): before booting the
chatbot, `run.sh` force-refreshes the gateway's discovery cache
(`.tally/models.json`, CTO-109) with `TALLY_MODELS_REFRESH=1` and logs the live
OpenAI/Anthropic lineup. This keeps the picker's IDs (`app/lib/ai/models.ts`,
which now pin `gpt-5` / `gpt-5-mini`; the retired `gpt-4o` / `gpt-4o-mini` SKUs
are gone) and `providers.ts`'s `resolveLatest()` fallbacks from rotting. The
refresh is **fail-soft**: offline / no API key just warns and proceeds on the
pinned IDs. For an offline or hermetic run, either set `TALLY_SKIP_MODEL_REFRESH=1`
to skip it, or point `TALLY_PINNED_MODELS=<path>` at a saved lineup (discovery
loads it verbatim and skips the network). The picker list itself is still
hardcoded; reading the cache dynamically in the model picker is a CTO-147 follow-up.

The chatbot boots on `:3001` (avoiding the dashboard on `:3000`). The driver
posts spans straight to the gateway from the chatbot's `/api/chat` route, so
this exercises the **gateway-POST ingestion path**, distinct from Aider's
edge-proxy path. After ~2 minutes, the dashboard auto-opens to
`/attribution?tag=chatbot-demo&outcome=positive_feedback`.

The run also emits **tool** spans (weather-style prompts trigger a `getWeather`
tool span) and, for ~30% of sessions, a simulated RAG-retrieval **embedding**
span. So on the **Cost** tab you'll now see LLM dominant with the **Tools** and
**Embeddings** bars non-zero (instead of $0), matching the seed mock. The tool
prices are a small fixed table (`getWeather`=$0.001, document tools=$0.005,
`requestSuggestions`=$0.002) and the embedding cost is computed at
text-embedding-3-small's $0.02/Mtok. Both are demo-seed values, not real
billing. (Vector/Compute/Egress layers stay $0: out of scope here.)

Walkthrough, configuration knobs, and the upstream patch list are in
[examples/vercel-chatbot/README.md](examples/vercel-chatbot/README.md) and
[examples/vercel-chatbot/PATCHES.md](examples/vercel-chatbot/PATCHES.md).

### Realistic-volume demo

The default `make chatbot-demo` drives **50** sessions (~$0.40), so a freshly
seeded stack shows a fraction-of-a-cent dashboard, not the **~$52,400/mo**
story the seed fixtures and the LinkedIn screenshots advertise. To reproduce
that startup-scale picture you have two paths:

**A. `$0` backfill (recommended for screenshots, and the repo's test corpus).**
Posts **30 days** of backdated synthetic spans + business events straight to the
gateway's `/v1/batches` endpoint. It makes **no LLM calls and needs no API
keys**, so a screenshot run costs **$0**. The LLM layer sums to ~$52,400 with the
seed feature mix (research_agent 54% · support_triage 17% · inline_writer 12% ·
smart_search 10% · chatbot 7%).

```bash
cd infra && make chatbot-demo-backfill
# tune it: make chatbot-demo-backfill BACKFILL_ARGS="--days 30 --target-usd 52400 --seed 138 --accounts 12"
```

The target refuses to run when the `local-dev` tenant UUID does not resolve, and
prints what to check. It used to drop `--tenant` in that case, which quietly
wrote 30 days of rows under the tenant NAME that the dashboard (which reads by
UUID) never rendered. There is no fallback to the name, by design.

What the corpus contains, and what each part is there to exercise:

| Part | Exercises |
|---|---|
| `llm` chat spans across 3 providers / 6 models (openai `gpt-5` + `gpt-4o-mini`, anthropic `claude-sonnet-4-5` + `claude-haiku-4-5`, google `gemini-2.5-pro` + `gemini-2.5-flash`) | Cost Explorer breakdowns by provider/model, Model Comparison, `/compare` |
| `tools` spans (tavily, serpapi, brave, firecrawl, exa, openai code_interpreter) at the catalog's per-call rates | The Tools layer bar, tool-cost drilldowns |
| `embeddings` spans (`text-embedding-3-small` / `-large`) | The Embeddings layer bar |
| `vector` spans (pinecone / weaviate / qdrant queries and upserts) | The Vector layer bar |
| daily `compute` and `egress` rows in the cloud-billing connector shape | The Compute/Egress layers and the accounts tab's *excluded infrastructure* pot |
| `AccountIdHash` on spans and on revenue events, ~12 accounts, top-heavy; ~8% of runs deliberately unattributed | Cost per Account, account margin, the unattributed bucket |
| 5 feature tags on 5 distinct agents (`ServiceName`) | `/features`, `/agents`, per-agent waste scoping |
| ~3.5% of runs failed with billed input tokens and no output | Recoverable Cost → **Failed but billed** |
| ~2.5% of runs failed and then retried successfully within 15-120s by a same-shape run | Recoverable Cost → **Duplicated work** |
| `conversion` (monetary) + `positive_feedback` (count, `value_amount_micro` NULL) events, carrying the account hash | `/attribution`, ROI, Value/user and Margin/user |

Honesty notes, so you know what the numbers are and are not:

- **All money is derived, never invented.** Rates mirror
  `sdk/python/src/tally/pricing.py:seed_catalog`, the script computes in integer
  micro-USD (BigInt, no float dollars), and the **gateway still recomputes the
  authoritative cost** from (provider, model, tokens) against that same catalog.
  The line the script prints is an expectation to check ClickHouse against, not
  the source of truth.
- **Account hashes come from the gateway**, via `POST /v1/tenant/account-lookup`,
  so they are the digests the tenant's own HMAC key produces and the
  cost-per-customer search box can resolve them. No raw customer id is ever put
  in a span. Labels are upserted through `POST /v1/tenant/account-labels` and
  live in Postgres, never in ClickHouse.
- **Layer sizes are honest, not flattering.** Only the LLM layer is sized to a
  dollar target. The per-call layers (tools, vector, embeddings) are sized by
  *span count* and then cost exactly what the catalog says, so the Vector bar is
  genuinely small: `$0.0004`/query is the serving portion only, and the deployed-
  index node-hours that dominate a real vector bill are a **compute** cost by
  design (see the cost-split note on `_VECTOR_SEEDS` in `pricing.py`).
  Compute/egress are daily aggregate bill rows, sized at 4.5% / 0.4% of the LLM
  layer.
- **Unknown stays unknown.** A failed call records 0 output tokens because that
  is what it produced, and a `count`-typed engagement event carries a NULL
  amount, not 0.
- Everything else is synthetic-seed: backdated timestamps, RNG-drawn token
  counts, fabricated conversion revenue, fictional company names.

The backfill is **idempotent**: batch ids are derived from `--seed`, so the
gateway's `(tenant_id, batch_id)` dedup makes a re-run a no-op. Use a fresh
`--seed` to layer in another independent month; the daily compute/egress span
ids are keyed on the seed too, so a layered month adds rows instead of colliding
with the previous one.

#### Checking the corpus actually landed

The one failure mode worth checking for is a tenant mismatch, so every query
below is scoped to the UUID. `make ch` opens the shell; `$TENANT` is the UUID
`make seed` printed.

```sql
-- Every layer present, under the UUID and not the name.
SELECT GenAiOperation, count() AS spans, round(sum(EstimatedCost), 2) AS usd,
       countIf(AccountIdHash != '') AS with_account
FROM otel_spans WHERE TenantId = '<uuid>' GROUP BY GenAiOperation ORDER BY usd DESC;

-- Accounts, biggest first. '' is the honest unattributed bucket, not a customer.
SELECT AccountIdHash, count(), round(sum(EstimatedCost), 2) AS usd
FROM otel_spans WHERE TenantId = '<uuid>' GROUP BY AccountIdHash ORDER BY usd DESC LIMIT 15;

-- Failed-but-billed runs (what the Recoverable Cost detector reads).
WITH runs AS (
  SELECT TraceId, sum(EstimatedCost) AS cost, max(StatusCode) AS st
  FROM otel_spans
  WHERE TenantId = '<uuid>' AND GenAiOperation NOT IN ('compute', 'egress')
  GROUP BY TraceId)
SELECT countIf(st = 2 AND cost > 0) AS failed_but_billed,
       round(sumIf(cost, st = 2), 2) AS recoverable_usd FROM runs;
```

If `TenantId` reads `local-dev` rather than a UUID anywhere in those results,
those rows predate the UUID switch and the dashboard will not render them.

**B. Live realistic mode (higher fidelity, real spend).** Drives ~**5,000**
sessions across the seed feature tags over a bounded window (~10 min), making
**real** OpenAI/Anthropic calls. Spend is bounded by a `--max-usd` cap
(default **$10**) so a laptop run stays ~$5–10.

```bash
cd infra && make chatbot-demo-realistic        # == make chatbot-demo MODE=realistic
# knobs (passed after --): window, cap, session count
bash examples/vercel-chatbot/run.sh -- --mode=realistic --max-usd 8 --window-min 10 --sessions 5000
```

The default `make chatbot-demo` (50 sessions, quick) is **unchanged**. Pick the
backfill for instant $0 screenshot data, or live realistic mode when you want
the cost numbers to come from real provider responses.

When you're done: `make chatbot-demo-stop` kills the chatbot dev server.

## Step 7: real revenue via Stripe

The chatbot demo emits synthetic conversion events so `/attribution` has
something to show. For **real** revenue numbers (Value/user, Margin/user, and
margin % per provider), connect Stripe (CTO-110). The gateway exposes a
verified webhook ingest at:

```
POST http://localhost:8080/v1/stripe/webhook?tenant=<your-tenant-id>
```

To wire it up:

1. Open `/connectors` in the dashboard and use the **Stripe revenue** tile to
   paste your signing secret (`whsec_…`). The raw secret is persisted on the
   gateway and never round-tripped back to the browser.
2. In the Stripe Dashboard → Developers → Webhooks, point a new endpoint at
   the URL above and subscribe to four events:
   `checkout.session.completed`, `invoice.paid`, `charge.refunded`,
   `customer.subscription.deleted`.
3. For local testing, run `stripe listen --forward-to http://localhost:8080/v1/stripe/webhook?tenant=local-dev`
   and paste the printed `whsec_…` into the tile.
4. (Optional) Backfill history with the helper:

   ```bash
   cd infra/gateway
   uv run python scripts/backfill_stripe.py \
       --tenant local-dev --days 30 \
       --stripe-key sk_live_xxx        # or sk_test_xxx
   ```

   The script is safe to re-run: idempotency is keyed on Stripe's event id.

Once events start landing, `/attribution` lights up two new columns:
**Value/user** and **Margin/user** (with margin % below). Cells stay `—`
until enough events arrive: we never fabricate numbers from absent data.

As a side-effect, the **Stripe card** in the third-party integrations section of
`/connectors` flips from "Not connected" to a green "Connected" card showing
real `last_run_at` and rolling 24h / 7d event counts (CTO-117). The Stripe
webhook handler calls `tenant_integration_runs.record_run` after each successful
insert, so the card stays current with no extra config. Segment / HubSpot /
Pendo cards remain "Not connected" until their workers land.

## Step 8: real cross-provider projections via replay

Workflows 2 (Compare) and 5 (Estimate) used to scale a mock projection off
the user's live current-model spend. **Opt in to replay** to back those
projections with real cross-provider calls instead (CTO-113).

1. **Enable replay sampling** for the local tenant:

   ```bash
   curl -X POST http://localhost:8080/v1/tenant/replay/config \
     -H 'x-tenant-id: local-dev' -H 'content-type: application/json' \
     -d '{"enabled": true, "sample_rate": 0.05, "daily_budget_usd": 5.0}'
   ```

   - `enabled` defaults to `false` for every tenant: no surprises.
   - `sample_rate` is the fraction of ingested spans we capture (default 5%).
   - `daily_budget_usd` is a **hard cap** on the replay executor's spend per
     tenant per day. A bug in replay must never burn $10k overnight; the
     executor checks today's spend before every candidate call and skips
     with `excluded_budget=True` when projected next-call cost would push the
     day over.

2. **Drive traffic** (`make chatbot-demo` or `make aider-demo`). The gateway
   stratifies the batch by `(feature_tag, token-quintile)` so small-but-
   expensive runs aren't drowned out, scrubs PII (emails, API keys, postal
   addresses), and writes the resolved request envelope to object storage.

3. **Request a projection**: the dashboard does this automatically from
   `/compare` and `/estimate`, but you can hit the gateway directly to see
   the raw output:

   ```bash
   curl -X POST http://localhost:8080/v1/replay \
     -H 'x-tenant-id: local-dev' -H 'content-type: application/json' \
     -d '{
       "candidate_models": [
         {"provider": "anthropic", "model": "claude-haiku-4-5"},
         {"provider": "openai",    "model": "gpt-5-mini"}
       ],
       "sample_size": 50
     }'
   ```

   Returns per-candidate `projected_monthly_cost_micro_usd`, `p50_latency_ms`,
   `p95_latency_ms`, `error_rate`, `samples_replayed`, and
   `excluded_budget_count`. Diagnostics carry the v1 honesty string
   `"resolved-context replay (no live retrieval)"` so the dashboard never
   claims a fidelity tier it doesn't have.

`/compare` and `/estimate` report `replay_source: "replay"` when the
projection is replay-backed and `"mock"` when it falls back to the rescaled
mock (tenant hasn't opted in yet, or the gateway is unreachable).

## Step 9: cross-provider eval (real quality scores)

Replay gives you per-candidate cost / latency / error from real calls, but
`/compare`'s **Quality** column still needs a judgment: "is the haiku-4.5
response actually as good as sonnet-4.5's was?". CTO-114 adds a pairwise
LLM-judge eval pass that replaces the previously fabricated `qualityScore`
with a real win-rate (and Wilson 95% CI). Opt in **separately** from replay
judge calls run a frontier model and are pricier than candidate replays.

1. **Enable eval** for the local tenant:

   ```bash
   curl -X POST http://localhost:8080/v1/tenant/eval/config \
     -H 'x-tenant-id: local-dev' -H 'content-type: application/json' \
     -d '{"enabled": true, "judge_model": "claude-opus-4-8", "daily_budget_usd": 10.0}'
   ```

   - Default off, default budget `$10/day`, default judge `claude-opus-4-8`.
   - The judge is overridable per tenant (e.g. to mitigate judge-self-bias if
     all candidates are claude-family; v2 will rotate judges automatically).
   - The daily budget is a hard ceiling enforced before every judge call.

2. **Run the eval pass** (or let the dashboard call it automatically):

   ```bash
   curl -X POST http://localhost:8080/v1/eval \
     -H 'x-tenant-id: local-dev' -H 'content-type: application/json' \
     -d '{
       "candidate_models": [
         {"provider": "anthropic", "model": "claude-haiku-4-5"},
         {"provider": "openai",    "model": "gpt-5-mini"}
       ],
       "sample_size": 50
     }'
   ```

   For each candidate, the executor pairs the candidate's replay response
   with the originally captured response, randomizes A/B order to mitigate
   position bias, and asks the judge for exactly `A`, `B`, or `TIE`. Anything
   else parses to an `error` row (the win-rate denominator excludes errors).

3. **Read the result on `/compare`.** The Quality column now shows
   `47.2%` with `[31–63%]` underneath: the real win-rate and Wilson 95% CI.
   Below the 10-judged-samples floor (small `n` means a CI wider than the
   number is useful), the cell shows `—` with the hint "needs ≥10 judged
   samples: run eval pass". **The page will never fabricate a quality
   number**: there is no fallback to mock here, by design.

   The `current` row's Quality is always `—`: a model is never paired against
   itself, so there's no judge verdict to surface.

   **Bias caveats** baked into the rubric (see `eval_executor.py`):

   - **Position bias**: A/B order is randomized per sample. The recorded
     verdict is decoded against the orientation we showed the judge.
   - **Judge-self-bias**: when one of the candidates is from the same model
     family as the judge (e.g. claude judging claude), the judge tends to
     slightly favor its own family. v1 accepts the trade-off; rotate the
     `judge_model` per tenant if the bias matters for a given comparison.
   - **Rubric versioning**: the prompt is tagged `rubric-v1`. A future
     tightening will bump the version so a mixed corpus stays interpretable.

## Live updates

Every dashboard page (Home, Agents, Cost, Attribution) auto-refreshes in the
browser on a short interval; leave the tab open while you run demos and new
spans appear without a manual reload (CTO-108). Pages still server-render the
first paint; a small client wrapper polls the same `/api/...` endpoint and
re-renders the body on each tick.

Knobs:

- `NEXT_PUBLIC_TALLY_DASHBOARD_REFRESH_MS`: poll interval, default `5000`.
  Set to `0` to disable polling entirely (the page becomes static again).
- Polling pauses automatically when the tab is hidden, and fetches once
  immediately on focus: no wasted requests sitting in a background tab.
- On transient API errors the page keeps the last good data and logs the
  error to `console.warn`; the badge stays green so a 5xx never blanks the UI.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Database tally does not exist` | Gateway pointed at the wrong DB. Use `TALLY_CLICKHOUSE_DB=default` (the compose gateway already does). |
| Dashboard shows the **"mock data"** badge | A page's ClickHouse query failed and fell back to mock. Check `make logs` and that step 3's count is non-zero. |
| Dashboard empty despite ingested rows | Tenant mismatch. The UI reads the tenant **UUID**, not the name, so spans must be tagged with the UUID: re-send with `tenant_id` set to the UUID from step 3. With no Clerk account, point the web app at that same UUID via `TALLY_DEV_TENANT` (the dev escape hatch); `TALLY_TENANT_ID` is no longer read by anything. |

## Make targets (run from `infra/`)

| Target | Does |
|---|---|
| `make up` | Start the full stack (build gateway image) |
| `make seed` | Create the `local-dev` tenant + API key |
| `make demo` | Send a sample batch through the gateway into ClickHouse |
| `make aider-demo` | Run Aider against a fixture repo through the edge proxy (needs `OPENAI_API_KEY`) |
| `make aider-demo-stop` | Kill the background edge proxy started by `aider-demo` |
| `make chatbot-demo` | Run the Vercel AI chatbot demo (needs `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`; `MODE=realistic` for startup volume) |
| `make chatbot-demo-realistic` | Live ~5000-session run over ~10 min, real LLM spend capped ~$10 |
| `make chatbot-demo-backfill` | `$0` 30-day backfill of synthetic backdated spans (no LLM calls, no API keys) |
| `make chatbot-demo-stop` | Kill the background chatbot dev server started by `chatbot-demo` |
| `make ps` / `make logs` | Status / tail gateway logs |
| `make ch` / `make psql` | ClickHouse / Postgres SQL shell |
| `make down` | Stop the stack (keep data volumes) |
| `make nuke` | Stop **and wipe** volumes (DDL re-applies on next `up`) |

## Tear down

```bash
cd infra && make down     # keep data
cd infra && make nuke     # wipe volumes
```

The web dev server is a foreground process; stop it with Ctrl-C in its terminal.
