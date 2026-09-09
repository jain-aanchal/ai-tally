# ai-tally local infra. Run from the infra/ directory:  make up / seed / demo / logs / down / nuke
COMPOSE := docker compose
GATEWAY := ai-tally-gateway

.PHONY: help up down nuke logs ps seed demo pg-migrate ch-migrate ch-migrate-otel-engine aider-demo aider-demo-stop chatbot-demo chatbot-demo-realistic chatbot-demo-backfill chatbot-demo-stop gateway-shell test ch psql

EDGE_PROXY_PID := /tmp/ai-tally-aider-edge-proxy.pid

# Honor the same POSTGRES_USER / POSTGRES_DB that .env.example exposes, so a dev running a
# non-default user does not get told to `make up && make seed` after both already succeeded.
PG_USER := $(or $(POSTGRES_USER),tally)
PG_DB := $(or $(POSTGRES_DB),tally)

# Canonical TenantId is the tenant UUID (Initiative 1, §8). Resolve the seeded local-dev tenant's
# UUID from Postgres so the demo backfill tags spans with the UUID the dashboard reads, not the
# name `local-dev`. Lazily expanded (`=`, not `:=`) so it runs when a target uses it, after the
# stack is up and `make seed` has created the row; empty before then. `:=` would shell out on every
# single make invocation, `make help` included. Recipes that need it more than once capture it into
# a shell variable so the lookup still runs exactly once per target.
TENANT_UUID = $(shell $(COMPOSE) exec -T postgres psql -U $(PG_USER) -d $(PG_DB) -tAc "SELECT id FROM tenants WHERE name='local-dev' LIMIT 1" 2>/dev/null | tr -d '[:space:]')

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the full stack (ClickHouse, Postgres, Redpanda, MinIO, gateway)
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d --build
	@echo "gateway:    http://localhost:8080/healthz"
	@echo "clickhouse: http://localhost:8123   minio console: http://localhost:9001"

down: ## Stop the stack (keep data volumes)
	$(COMPOSE) down

nuke: ## Stop and wipe all data volumes (DDL re-applies on next up)
	$(COMPOSE) down -v

# Directory replayed by pg-migrate. Overridable so the failure stop below can be exercised
# against a scratch directory without pointing psql at the real migration set.
PG_MIGRATIONS_DIR := ../db/postgres

pg-migrate: ## Apply db/postgres migrations to a RUNNING stack (initdb only fires on a fresh volume)
	@# Same gap as ch-migrate below: the /docker-entrypoint-initdb.d mounts in docker-compose.yml
	@# run ONLY on a first boot against an empty volume, so an already-running stack never picks up
	@# a new migration. That is how 0009/0010/0011/0012/0015/0016 stayed unapplied for months
	@# (CTO-317). Every file in db/postgres is idempotent (CREATE ... IF NOT EXISTS, ADD COLUMN IF
	@# NOT EXISTS), so replaying the whole directory is a no-op over a populated database.
	@#
	@# PG_USER / PG_DB rather than a hardcoded `tally`: a dev running POSTGRES_USER=ai_tally would
	@# otherwise get `FATAL: role "tally" does not exist` on every file in the set.
	@#
	@# ON_ERROR_STOP=1 stops psql inside one file; the `|| { ...; exit 1; }` is what stops the LOOP.
	@# Without it a failure scrolls past the remaining files and the operator walks away from a
	@# partially applied schema believing the replay succeeded.
	@#
	@# Replay order is LC_ALL=C, which is NOT initdb's order: postgres:16 runs its entrypoint glob
	@# under LANG=en_US.utf8, where glibc collation ignores the underscore and sorts
	@# 0005a_tenant_eval_config.sql BEFORE 0005_cac_periods.sql. The two 0005 tables are
	@# independent, so both orders work; see db/postgres/README.md before adding a dependent pair.
	@for f in $$(LC_ALL=C ls $(PG_MIGRATIONS_DIR)/*.sql); do \
	  echo "  applying $$f"; \
	  $(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U $(PG_USER) -d $(PG_DB) -q < $$f \
	    || { echo ""; echo "FAILED on $$f - stopping. The schema is PARTIALLY applied: fix that"; \
	         echo "file and re-run 'make pg-migrate' (replaying is idempotent)."; exit 1; }; \
	done
	@echo "Postgres migrations applied."

# Canonical ClickHouse DDL, in the same dependency order compose mounts it into initdb.
CH_DDL := ../db/clickhouse/otel_spans.sql ../db/clickhouse/rollups.sql \
          ../db/clickhouse/account_rollups.sql \
          ../db/clickhouse/last_touch_index.sql ../db/clickhouse/attribution.sql \
          ../db/clickhouse/replay_samples.sql

ch-migrate: ## Apply db/clickhouse DDL to a RUNNING stack (initdb only fires on a fresh volume)
	@# The /docker-entrypoint-initdb.d mounts in docker-compose.yml run ONLY on a first boot
	@# against an empty volume. Anyone whose stack is already up therefore never sees a new
	@# column added to those files, which is how the Postgres side of this repo ended up with
	@# migrations 0011/0012/0015/0016 that had silently never been applied. This target closes
	@# that gap for ClickHouse: every statement in db/clickhouse is CREATE ... IF NOT EXISTS or
	@# ALTER ... ADD COLUMN IF NOT EXISTS, so replaying the whole set is idempotent, cheap
	@# (metadata-only for additive columns) and safe to run against a populated database.
	@for f in $(CH_DDL); do \
	  echo "  applying $$f"; \
	  $(COMPOSE) exec -T clickhouse clickhouse-client -u tally --password tally -d default \
	    --multiquery < $$f || exit 1; \
	done
	@echo "ClickHouse DDL applied."

ch-migrate-otel-engine: ## One-shot: move an EXISTING otel_spans to ReplacingMergeTree (CTO-245)
	@# Deliberately NOT part of ch-migrate. ClickHouse cannot ALTER a table's engine or ORDER BY,
	@# so the only migration is create-copy-swap, which is a full copy of the raw span table and
	@# is neither cheap nor safe to run implicitly on every replay. A fresh database already gets
	@# the right engine from otel_spans.sql; this target is how an install that predates CTO-245
	@# catches up. Read the header of the script first: it needs disk for a second full copy and
	@# ingest should be quiesced while it runs.
	@echo "Current engine:"
	@$(COMPOSE) exec -T clickhouse clickhouse-client -u tally --password tally -d default \
	  -q "SELECT engine, sorting_key FROM system.tables WHERE database = currentDatabase() AND name = 'otel_spans'"
	@$(COMPOSE) exec -T clickhouse clickhouse-client -u tally --password tally -d default \
	  --multiquery < ../db/clickhouse/migrations/otel_spans_replacing_engine.sql
	@echo "Engine after migration:"
	@$(COMPOSE) exec -T clickhouse clickhouse-client -u tally --password tally -d default \
	  -q "SELECT engine, sorting_key FROM system.tables WHERE database = currentDatabase() AND name = 'otel_spans'"
	@echo "Rows vs distinct spans (these should now match):"
	@$(COMPOSE) exec -T clickhouse clickhouse-client -u tally --password tally -d default \
	  -q "SELECT count() AS rows, uniqExact(TenantId, TraceId, SpanId) AS spans FROM otel_spans"
	@echo "The pre-migration table is kept as otel_spans_cto245. Drop it once you are satisfied."

logs: ## Tail the gateway logs
	$(COMPOSE) logs -f gateway

ps: ## Show stack status
	$(COMPOSE) ps

seed: ## Create a local tenant + API key + feature tags in Postgres
	$(COMPOSE) exec gateway python -m gateway.seed
	@echo ""
	@echo "Canonical TenantId is the tenant UUID (Initiative 1, §8). To run the dashboard against"
	@echo "this tenant with NO Clerk account, set the dev escape hatch (§10):"
	@echo "  export TALLY_DEV_TENANT=$(TENANT_UUID)"

demo: ## Send a sample batch through the gateway into ClickHouse
	@# send_batch.py defaults to the tenant NAME, but ingest writes TenantId verbatim while the
	@# dashboard reads by UUID (Initiative 1, §8), so the default would land rows the UI never shows.
	@# Fail loudly rather than write invisible rows: an empty UUID means the tenant is not seeded.
	@# One expansion of TENANT_UUID, captured in a shell variable: the guard and the send must agree,
	@# and a second expansion would be a second round trip into psql.
	@t="$(strip $(TENANT_UUID))"; \
	 test -n "$$t" || { \
	   echo "cannot resolve the local-dev tenant UUID from Postgres; run 'make up && make seed' first"; \
	   exit 1; }; \
	 set -x; $(COMPOSE) exec gateway python /app/infra/gateway/examples/send_batch.py --tenant "$$t"

aider-demo: ## Run Aider against a fixture repo through the edge proxy (needs OPENAI_API_KEY)
	@curl -sf http://localhost:8080/healthz >/dev/null || { \
	  echo "ai-tally stack not up — run 'make up && make seed' first"; exit 1; }
	@PROVIDER=$${PROVIDER:-openai} bash ../examples/aider-demo/run.sh

aider-demo-stop: ## Stop the background edge proxy started by aider-demo
	@if [ -f $(EDGE_PROXY_PID) ]; then \
	  kill $$(cat $(EDGE_PROXY_PID)) 2>/dev/null || true; \
	  rm -f $(EDGE_PROXY_PID); \
	  echo "edge-proxy stopped"; \
	else echo "no edge-proxy PID file at $(EDGE_PROXY_PID)"; fi

CHATBOT_PID := /tmp/ai-tally-chatbot.pid

chatbot-demo: ## Run the Vercel AI chatbot demo (needs OPENAI_API_KEY, ANTHROPIC_API_KEY; pass MODE=realistic for startup volume)
	@curl -sf http://localhost:8080/healthz >/dev/null || { \
	  echo "ai-tally stack not up — run 'make up && make seed' first"; exit 1; }
	@MODE=$(MODE) bash ../examples/vercel-chatbot/run.sh

chatbot-demo-realistic: ## Live realistic-volume run (~5000 sessions/~10min, REAL LLM spend capped ~$10)
	@$(MAKE) chatbot-demo MODE=realistic

chatbot-demo-backfill: ## $0 backfill — POST 30 days of backdated synthetic spans to match the seed story (no LLM calls, no API keys)
	@curl -sf http://localhost:8080/healthz >/dev/null || { \
	  echo "ai-tally stack not up — run 'make up && make seed' first"; exit 1; }
	@# Same refusal as `make demo` above, for the same reason (Initiative 1, §8). This target used
	@# to drop --tenant entirely when the UUID would not resolve, so the backfill fell back to its
	@# own `local-dev` default: 30 days of spans landed in ClickHouse under the tenant NAME, the
	@# dashboard read by UUID, matched nothing, and rendered empty with no error anywhere. Never a
	@# silent fallback; fail with the thing to check.
	@test -n "$(strip $(TENANT_UUID))" || { \
	  echo "ERROR: could not resolve the 'local-dev' tenant UUID from Postgres."; \
	  echo ""; \
	  echo "       The dashboard reads spans by tenant UUID, so there is no safe fallback here:"; \
	  echo "       backfilling under the tenant NAME would write 30 days of rows nothing renders."; \
	  echo ""; \
	  echo "       Check, in order:"; \
	  echo "         1. Postgres is up:    make ps"; \
	  echo "         2. The tenant exists: make psql  ->  SELECT id, name FROM tenants;"; \
	  echo "         3. If it is missing, seeding never ran or failed: make seed"; \
	  exit 1; }
	@cd ../examples/vercel-chatbot/app && \
	  { [ -d node_modules ] || pnpm install --silent --prefer-offline || \
	    npm install --silent --no-audit --no-fund; } && \
	  TALLY_GATEWAY_URL="$${TALLY_GATEWAY_URL:-http://localhost:8080/v1/batches}" \
	  pnpm --silent exec tsx ../scripts/backfill-spans.ts --tenant $(TENANT_UUID) $(BACKFILL_ARGS)

chatbot-demo-stop: ## Stop the background chatbot dev server started by chatbot-demo
	@if [ -f $(CHATBOT_PID) ]; then \
	  kill $$(cat $(CHATBOT_PID)) 2>/dev/null || true; \
	  rm -f $(CHATBOT_PID); \
	fi
	@pids=$$(lsof -ti :3001 2>/dev/null); \
	if [ -n "$$pids" ]; then \
	  kill -9 $$pids 2>/dev/null || true; \
	  echo "chatbot dev server stopped"; \
	else echo "nothing listening on :3001"; fi

test: ## Run gateway unit tests (pure mapping logic, no infra)
	cd gateway && uv run --extra dev pytest -q

ch: ## Open a ClickHouse SQL shell
	$(COMPOSE) exec clickhouse clickhouse-client -u tally --password tally -d default

psql: ## Open a Postgres shell
	$(COMPOSE) exec postgres psql -U $(PG_USER) -d $(PG_DB)

gateway-shell: ## Shell into the gateway container
	$(COMPOSE) exec gateway bash
