#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# ai-tally demo-deploy-kit - reset + re-seed the SYNTHETIC demo data (CTO-243).
#
# Run nightly so the demo always shows a fresh, backdated "last 30 days". It:
#   1. TRUNCATEs the ClickHouse telemetry tables (spans, events, rollups, replay corpus).
#   2. Re-runs `make seed` (idempotent: tenant + API key + price catalog).
#   3. Re-POSTs 30 days of backdated synthetic spans via the backfill script.
#
# The stack keeps running throughout (no container restart); only the data is reset. The Postgres
# control plane (tenant row, API key, connector config) is left intact.
#
# WHY truncate first: the backfill dedups by a deterministic batch_id with a 24h TTL. After 24h the
# dedup cache has expired, so a second run would double-count unless the prior rows are cleared.
#
# Cron example (nightly at 03:15, logging to a file) - `crontab -e` on the VM:
#
#   15 3 * * * /opt/ai-tally/deploy/demo/reseed.sh >> /var/log/ai-tally-reseed.log 2>&1
#
# (Point the path at wherever you checked the repo out on the VM.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

ENV_FILE="deploy/demo/.env"
BASE_COMPOSE="infra/docker-compose.yml"
PROD_COMPOSE="deploy/demo/docker-compose.prod.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found. Copy deploy/demo/.env.example to ${ENV_FILE} first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${BASE_COMPOSE}" -f "${PROD_COMPOSE}")

CH_USER="${CLICKHOUSE_USER:-tally}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-tally}"

# Data-bearing telemetry tables (the _mv views are triggers with no storage of their own, so
# clearing their target tables is enough).
TABLES=(
  otel_spans
  business_events
  attribution_records
  unattributed_events
  last_touch_index
  identity_graph
  daily_account_rollup
  daily_feature_rollup
  hourly_feature_rollup
  eval_runs
  replay_runs
  replay_samples
)

echo "==> Truncating ClickHouse telemetry tables"
for t in "${TABLES[@]}"; do
  echo "    TRUNCATE ${t}"
  "${COMPOSE[@]}" exec -T clickhouse \
    clickhouse-client -u "${CH_USER}" --password "${CH_PASSWORD}" -d default \
    --query "TRUNCATE TABLE IF EXISTS ${t}"
done

echo "==> Re-seeding the demo tenant (make seed)"
make -C infra COMPOSE="docker compose --env-file ${REPO_ROOT}/${ENV_FILE} -f ${REPO_ROOT}/${BASE_COMPOSE} -f ${REPO_ROOT}/${PROD_COMPOSE}" seed

echo "==> Re-backfilling 30 days of SYNTHETIC demo spans"
# Same throwaway-container approach as deploy.sh: no host Node, reaches the gateway internally.
COMPOSE_NETWORK="${COMPOSE_NETWORK:-ai-tally_default}"
docker run --rm \
  --network "${COMPOSE_NETWORK}" \
  -v "${REPO_ROOT}/examples/vercel-chatbot/scripts:/scripts:ro" \
  -e TALLY_GATEWAY_URL="http://gateway:8080/v1/batches" \
  node:22-bookworm-slim \
  npx --yes tsx /scripts/backfill-spans.ts

echo "==> Demo data reset. The dashboard now shows a fresh synthetic 30-day window."
