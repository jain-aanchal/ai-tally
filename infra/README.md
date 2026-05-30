# ai-tally local infrastructure

A one-command local stack that mirrors the cloud topology. Everything runs in Docker on a laptop;
the move to managed services later is a config change, not a rewrite.

## What's in the box

| Service    | Role                                   | Local port            |
|------------|----------------------------------------|-----------------------|
| ClickHouse | telemetry store (spans, rollups, attribution) | 8123 (HTTP), 9000 (native) |
| Postgres   | control plane (tenants, keys, guardrails)     | 5432                  |
| Redpanda   | ingest buffer (Kafka API)              | 9092                  |
| MinIO      | object / warm storage (S3 API)         | 9002 (API), 9001 (console) |
| Gateway    | SDK batch → enrich → ClickHouse (FastAPI) | 8080               |

The canonical DDL in [`../db`](../db) is mounted into the containers and applied automatically on
first boot — `otel_spans` and the rollup MVs into ClickHouse, the control-plane schema into Postgres.

> The Go edge proxy (transparent OpenAI passthrough) is intentionally **not** part of the local
> stack — the SDK ingestion path covers end-to-end flow without it. It's a later, optional addition.

## Prerequisites

- **Docker Desktop** (the only missing prerequisite on a fresh Mac). Install from docker.com.
- That's it. Python/Node already on the machine; the gateway builds inside Docker.

## Quickstart

```bash
cd infra
make up        # build + start everything (first run pulls images, ~2-3 min)
make seed      # create a local tenant + API key + feature tags
make demo      # push a sample batch through the gateway into ClickHouse
make ch        # SQL shell (default db) — try: SELECT FeatureTag, count(), sum(EstimatedCost) FROM otel_spans GROUP BY FeatureTag
make down      # stop (keep data)   |   make nuke = stop + wipe volumes
```

Configuration lives in `.env` (auto-created from `.env.example` on first `make up`). All values are
local-only defaults — nothing here is a real secret.

## The gateway

`POST /v1/batches` accepts a [`tally.wire.BatchRequest`](../sdk/python/src/tally/wire.py) JSON
envelope and, reusing the SDK's already-tested pure logic:

1. **authenticates** (optional) — `TALLY_REQUIRE_API_KEY=true` requires `Authorization: Bearer <key>`
   whose SHA-256 is registered in `api_keys` (raw keys are never stored);
2. **dedupes** idempotently on `(tenant_id, batch_id)` — replays return the original response;
3. **enriches cost** authoritatively from the price catalog (client cost kept only as a drift hint);
4. **clamps clock skew** so a fast client can't poison time-bucketed rollups;
5. **writes** spans → `otel_spans`, business events → `business_events`, identity links →
   `identity_graph`.

Health: `GET /healthz` (liveness), `GET /readyz` (checks ClickHouse + Postgres).

Run the mapping unit tests without any infra:

```bash
cd infra/gateway && uv run --extra dev pytest -q
```

## Cost

- **Local: $0** — all images are free OSS; ~3–4 GB RAM while running.
- **Managed cloud** (when you outgrow local): see
  [`../docs/adr/0001-clickhouse-managed-vs-self-hosted.md`](../docs/adr/0001-clickhouse-managed-vs-self-hosted.md)
  — ~$200–400/mo at MVP, scaling with volume.

## Wiring the web dashboard at real data

Point the Next.js app's API base at the gateway (or a future read API) via
`NEXT_PUBLIC_API_BASE_URL`. Today the web app reads from its own mock Route Handlers; swapping those
to query ClickHouse is the next increment.
