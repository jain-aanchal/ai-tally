# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What ai-tally is

Cost-and-value observability for AI products. It meters what an AI feature costs across every layer (LLM tokens, vector search, tool calls, embeddings, compute, egress), attributes that spend to features and to the tenant's own customers, and joins it to business value so a team can see what their AI actually costs and whether it pays for itself.

## Layout

Four codebases plus infra and schema:

- `sdk/python/`: the Python SDK (`tally.*`): span emission, per-tenant HMAC user/account hashing, pricing catalog, guardrail engine, the pure in-memory attribution stitcher, CDP/revenue connectors. `uv`, `ruff`, `pytest`.
- `infra/gateway/`: the FastAPI ingest gateway: `/v1/batches` ingest, the control plane (`/v1/tenant/*`), cloud cost connectors, the scheduler that runs per-tenant jobs, third-party ingest workers, reconciliation, BigQuery/Athena export. `uv`, `ruff`, `pytest`.
- `infra/edge-proxy/`: the Go edge proxy: language-agnostic metering in the request path. `go build`, `go test`, `gofmt`.
- `web/`: the Next.js 15 dashboard (App Router). Reads ClickHouse directly for telemetry and calls the gateway for control-plane writes. `npm run typecheck | lint | test` (vitest).
- `db/postgres/`: numbered control-plane migrations (`0001_...` upward). `db/clickhouse/`: telemetry table DDL.
- `infra/docker-compose.yml`: ClickHouse, Postgres, Redpanda, MinIO, gateway. `infra/Makefile` drives it.

## Running it

From `infra/`: `make up` (stack), `make seed`, `make chatbot-demo-backfill` (populate demo data), then `cd web && npm run dev` (dashboard on :3000, the `.claude/launch.json` "web" config). `make ch-migrate` replays ClickHouse DDL; `make ps` / `make logs` / `make psql` / `make ch` are the usual handles. See `RUNNING.md` for the full walkthrough.

## Invariants that are not negotiable

These are enforced throughout and reviews reject violations:

- **Honest under uncertainty.** Render a blank (a real reason on hover) rather than a fabricated or zero number. A value that is unknown is `null`/`—`, never `0`. A failed job records `failed` and emits nothing, never a guessed figure.
- **No bodies in telemetry.** Counts, hashes and mapped events only. No prompts, completions or retrieved text reach storage. PII is scrubbed from every error routed to storage.
- **Identifiers by hash, credentials by reference.** User and account ids are HMAC-SHA256'd under a per-tenant key so a hash cannot be reversed or joined across tenants. Secrets are Secret Manager / KMS / ARN references, never raw keys, enforced by length-bounded CHECK constraints.
- **Money is integer micro-USD.** Never float dollars. Rate math uses `Decimal` (Python) or BigInt (TS); convert at the boundary only.

## Conventions

- Source files start with `// SPDX-License-Identifier: Apache-2.0` (or the language's comment form).
- Comments explain WHY, not what, and cite the ticket (e.g. `CTO-176`). Match the surrounding density and voice.
- Prose, comments and docs use no em dashes. (The `—` glyph the honest-blank UI component renders is a real UI character and is exempt.)
- Control-plane writes go through gateway endpoints; the web app never touches Postgres directly. New per-tenant config follows the existing store pattern (see `gateway/connectors/config_admin.py`, `gateway/tenant_budgets.py`).
- Postgres migrations are strictly numbered; take the next free number and add the `infra/docker-compose.yml` mount. `docker-entrypoint-initdb.d` only runs on a first boot against an empty volume, so apply a new migration by hand to test against a running stack.
- Tenant identity: the dashboard passes the tenant NAME (`local-dev`), control-plane tables key on `tenants.id` (UUID). Resolve with `gateway.tenant_lookup.resolve_tenant_uuid`; do not feed a name into a UUID column.

## Verification

CI must stay green. Before shipping a change, run the affected project's checks:

- gateway: `cd infra/gateway && uv run --extra dev pytest -q && uv run ruff check .` (use `--extra dev` on a cold checkout or collection fails on missing deps).
- sdk: `cd sdk/python && uv run --extra dev pytest -q && uv run ruff check .`.
- web: `cd web && npm run typecheck && npm run lint && npm run test`.
- edge-proxy: `cd infra/edge-proxy && go build ./... && go test ./... && gofmt -l .` (gofmt must list nothing).

Known: two `web/app/api/api.test.ts` cases ("falls back to mock when ClickHouse is unreachable") fail whenever a local ClickHouse IS running, because they assert the unreachable path. They pass in CI. Introduce no new failures beyond those two.
