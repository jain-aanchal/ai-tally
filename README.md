# ai-tally

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
db/clickhouse/     ClickHouse DDL (telemetry store)
```

More components (edge proxy, ingest gateway, web app) land as they're built.

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
