# ai-tally

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Cost-and-value observability for AI products. See what your AI actually costs — all-in — and what it returns.

Five workflows on one shared data spine:

1. **Agent loop cost visibility** — why did this run cost 50× median?
2. **Cross-provider comparison** — are we on the right model?
3. **End-to-end cost** — what does this feature really cost, all-in?
4. **Business-outcome attribution** — is this AI feature profitable?
5. **Pre-deploy estimation** — what will this change cost before we ship?

## Product principles

- **Honest under uncertainty** — estimated vs. reconciled, confidence shown everywhere.
- **Tail-aware, not median-aware** — agent cost is a power law; optimize for p99.
- **Never corrupt customer state** — guardrails degrade gracefully, never hard-kill.
- **OTel-native** — built on OpenTelemetry `gen_ai.*` conventions; contribute upstream.
- **Time-to-value < 30 min** — proxy in 5, SDK in 30.

## Repository layout

```
sdk/python/        Python SDK (OTel gen_ai.* + cost/feature/identity extensions)
infra/gateway/     Ingest gateway (FastAPI: auth → enrich cost → ClickHouse)
infra/edge-proxy/  Zero-code edge proxy (Go) + BYO-deployment Helm chart
infra/             docker-compose stack (ClickHouse, Postgres, Redpanda, MinIO) + Makefile
db/clickhouse/     ClickHouse DDL (telemetry store)
db/postgres/       Postgres control-plane schema
web/               Next.js dashboard (the five workflows)
```

## Running it

To bring up the whole stack on a laptop and see ingested telemetry in the dashboard, follow
**[RUNNING.md](./RUNNING.md)** — a verified end-to-end runbook (send a batch → gateway → ClickHouse
→ web UI). Short version:

```bash
cd infra && make up && make seed && make demo   # stack + tenant + sample telemetry
cd web && npm install && npm run dev            # dashboard at http://localhost:3000
```

## Development

The Python SDK uses [uv](https://docs.astral.sh/uv/), `ruff`, and `pytest`.

```bash
cd sdk/python
uv sync
uv run ruff check .
uv run pytest
```

## Status

Early development. Decisions and the full system spec live in the project tracker.
Tickets follow a Context / Acceptance criteria / Out-of-scope format and are picked up one PR at a time.

## License

ai-tally is licensed under the [Apache License, Version 2.0](LICENSE). Required
attribution notices for the project and any third-party dependencies live in
[NOTICE](NOTICE).

We follow the [Contributor Covenant](CODE_OF_CONDUCT.md) Code of Conduct.
