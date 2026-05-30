# ai-tally local infra. Run from the infra/ directory:  make up / seed / demo / logs / down / nuke
COMPOSE := docker compose
GATEWAY := ai-tally-gateway

.PHONY: help up down nuke logs ps seed demo gateway-shell test ch psql

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

logs: ## Tail the gateway logs
	$(COMPOSE) logs -f gateway

ps: ## Show stack status
	$(COMPOSE) ps

seed: ## Create a local tenant + API key + feature tags in Postgres
	$(COMPOSE) exec gateway python -m gateway.seed

demo: ## Send a sample batch through the gateway into ClickHouse
	$(COMPOSE) exec gateway python /app/infra/gateway/examples/send_batch.py

test: ## Run gateway unit tests (pure mapping logic, no infra)
	cd gateway && uv run --extra dev pytest -q

ch: ## Open a ClickHouse SQL shell
	$(COMPOSE) exec clickhouse clickhouse-client -u tally --password tally -d default

psql: ## Open a Postgres shell
	$(COMPOSE) exec postgres psql -U tally -d tally

gateway-shell: ## Shell into the gateway container
	$(COMPOSE) exec gateway bash
