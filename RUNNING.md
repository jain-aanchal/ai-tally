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
                                            (:8123, db=default, TenantId=local-dev)
                                                 ▲
   browser ──▶ Next.js web (:3000) ──Route Handler──┘  (web/lib/clickhouse.ts, tenant=local-dev)
```

Everything is keyed to the **`local-dev`** tenant: the demo batch writes as `local-dev`, and the
web UI reads `local-dev` by default — they line up with zero configuration.

## Prerequisites

- Docker (Compose v2) — `docker version` should print a server version.
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

The composed gateway already sets `TALLY_CLICKHOUSE_DB=default` (the official ClickHouse image loads
unqualified DDL into the `default` database — see the note in `infra/docker-compose.yml`). Running
the gateway *by hand* with the library default `TALLY_CLICKHOUSE_DB=tally` will fail with
`Database tally does not exist`; pass `TALLY_CLICKHOUSE_DB=default` if you do.

## 2. Seed the local tenant

```bash
make seed     # creates the `local-dev` tenant + API key + feature tags in Postgres
```

This prints a one-time API key (`tally_sk_…`). Only its SHA-256 is stored — copy it if you plan to
enable auth. For local testing auth is **off** by default (`TALLY_REQUIRE_API_KEY=false`).

## 3. Push telemetry through the gateway

Easiest — the built-in demo batch (writes as tenant `local-dev`, and re-sends once to demonstrate
idempotent replay):

```bash
make demo            # run it a few times for more rows
```

Or fire your own burst (note `tenant_id` **must** be `local-dev` for the UI to show it):

```bash
for i in $(seq 1 40); do
  curl -s -X POST localhost:8080/v1/batches \
    -H 'content-type: application/json' \
    -d '{"tenant_id":"local-dev","sdk_version":"test","resource_spans":[
          {"trace_id":"tr'$i'","span_id":"s'$i'","gen_ai.system":"openai",
           "gen_ai.operation.name":"chat","gen_ai.request.model":"gpt-4o",
           "gen_ai.usage.input_tokens":1200,"gen_ai.usage.output_tokens":350}]}' >/dev/null
done
```

Verify the rows landed (and carry enriched cost):

```bash
curl -s 'http://localhost:8123/?user=tally&password=tally&database=default' \
  --data "SELECT count(), round(sum(EstimatedCost),4) FROM otel_spans WHERE TenantId='local-dev'"
```

or open a SQL shell with `make ch` and run
`SELECT FeatureTag, count(), sum(EstimatedCost) FROM otel_spans WHERE TenantId='local-dev' GROUP BY FeatureTag`.

## 4. Run the web dashboard

In a separate terminal:

```bash
cd web
npm install         # first run only
npm run dev
```

Open **http://localhost:3000**.

No env config is needed: `web/lib/clickhouse.ts` defaults to exactly what the stack uses —
`http://localhost:8123`, database `default`, tenant `local-dev`. Each Route Handler queries
ClickHouse live and falls back to mock data **only** if ClickHouse is unreachable. With the stack up
and a batch sent, the **Cost**, **Features**, **Agents**, and **Data Quality** pages render your
ingested `local-dev` spans.

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

then `make up` again. You can watch the guarantee directly: stop ClickHouse, fire a burst — POSTs
still return `200 accepted` while the drain loop logs `drain failed; retrying` and holds the rows
until ClickHouse returns. Knobs (all `TALLY_`-prefixed): `INGEST_BUFFER_CAPACITY` (default `200000`;
rows past this are shed as *retryable*, never 5xx), `INGEST_BUFFER_DRAIN_BATCH` (`2000`),
`INGEST_BUFFER_POLL_INTERVAL_S` (`0.05`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Database tally does not exist` | Gateway pointed at the wrong DB. Use `TALLY_CLICKHOUSE_DB=default` (the compose gateway already does). |
| Dashboard shows the **"mock data"** badge | A page's ClickHouse query failed and fell back to mock. Check `make logs` and that step 3's count is non-zero. |
| Dashboard empty despite ingested rows | Tenant mismatch — the UI reads `local-dev`. Post with `tenant_id: local-dev` (or set `TALLY_TENANT_ID` for the web app). |
| **"Partial data"** banner | Expected: only LLM spans exist, so vector/tools/compute cost layers are zero until those connectors are wired. Not an error. |

## Make targets (run from `infra/`)

| Target | Does |
|---|---|
| `make up` | Start the full stack (build gateway image) |
| `make seed` | Create the `local-dev` tenant + API key |
| `make demo` | Send a sample batch through the gateway into ClickHouse |
| `make ps` / `make logs` | Status / tail gateway logs |
| `make ch` / `make psql` | ClickHouse / Postgres SQL shell |
| `make down` | Stop the stack (keep data volumes) |
| `make nuke` | Stop **and wipe** volumes (DDL re-applies on next `up`) |

## Tear down

```bash
cd infra && make down     # keep data
cd infra && make nuke     # wipe volumes
```

The web dev server is a foreground process — stop it with Ctrl-C in its terminal.
