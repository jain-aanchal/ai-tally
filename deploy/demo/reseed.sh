#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# ai-tally demo-deploy-kit - reset + re-seed the SYNTHETIC demo data (CTO-243).
#
# Run nightly so the demo always shows a fresh, backdated "last 30 days". It:
#   1. Re-runs `make seed` (idempotent: tenant + API key + price catalog), so a stack whose control
#      plane was wiped bootstraps itself instead of failing every night at the resolve below.
#   2. Resolves the demo tenant's UUID, aborting if it cannot.
#   3. TRUNCATEs the ClickHouse telemetry tables (spans, events, rollups, replay corpus).
#   4. Re-POSTs 30 days of backdated synthetic spans via the backfill script.
#
# Steps 2 and 3 are in that order on purpose: never truncate without a resolved tenant to repost
# under (a failed resolve then leaves the previous night's data on screen).
#
# The stack keeps running throughout (no container restart); only the data is reset. The Postgres
# control plane (tenant row, API key, connector config) is left intact.
#
# WHY truncate first: the backfill dedups by a deterministic batch_id with a 24h TTL. After 24h the
# dedup cache has expired, so a second run would double-count unless the prior rows are cleared.
#
# WHY a fresh --seed on every reseed: the backfill derives its batch_ids deterministically from
# --seed, so a reseed run within 24h of a prior backfill (deploy or a same-day reseed) would post
# the SAME batch_ids, the gateway would dedup them, and TRUNCATE-then-repost would leave the
# dashboard blank. Seeding with a per-run nonce (the wall-clock epoch below) gives fresh batch_ids
# so the reposted rows always land. The dataset stays the same synthetic ~$52,400/mo story; only
# the RNG draws (and thus the batch_ids) differ (CTO-243).
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

# Tenant-UUID resolution and the service-token preflight, shared with deploy.sh.
# shellcheck source=deploy/demo/lib-tenant.sh
source "${SCRIPT_DIR}/lib-tenant.sh"

require_service_token_if_auth_on
warn_backfill_unsupported_if_auth_on

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

# Seed FIRST so a wiped control plane can bootstrap itself. `make seed` is idempotent, so on a
# normal nightly run this is a no-op, but on a stack whose Postgres volume was recreated (fresh VM,
# `make nuke`, a restore from an empty volume) it is the only thing that recreates the tenant row.
# Resolving before this point meant every cron run on such a host aborted at "could not resolve the
# tenant UUID" and could never reach the step that would have fixed it (CTO-243).
echo "==> Re-seeding the demo tenant (make seed)"
make -C infra COMPOSE="docker compose --env-file ${REPO_ROOT}/${ENV_FILE} -f ${REPO_ROOT}/${BASE_COMPOSE} -f ${REPO_ROOT}/${PROD_COMPOSE}" seed

# Then resolve, still BEFORE the TRUNCATE. That ordering is the safety property: if the control
# plane cannot answer even after seeding, a nightly run aborts with the old data still on screen
# rather than emptying the tables and only then discovering it has no tenant to repost under. Under
# `set -euo pipefail` a failed resolve_tenant_uuid aborts the script here, so no path reaches the
# TRUNCATE loop without a validated UUID (CTO-243).
echo "==> Resolving the demo tenant UUID"
TENANT_UUID="$(resolve_tenant_uuid)"
echo "    ${DEMO_TENANT_NAME} = ${TENANT_UUID}"

echo "==> Truncating ClickHouse telemetry tables"
for t in "${TABLES[@]}"; do
  echo "    TRUNCATE ${t}"
  "${COMPOSE[@]}" exec -T clickhouse \
    clickhouse-client -u "${CH_USER}" --password "${CH_PASSWORD}" -d default \
    --query "TRUNCATE TABLE IF EXISTS ${t}"
done

echo "==> Re-backfilling 30 days of SYNTHETIC demo spans"
# Same throwaway-container approach as deploy.sh: no host Node, reaches the gateway internally.
# --seed: a per-run nonce so batch_ids are fresh and the gateway's 24h dedup never drops a reseed
#         (see the WHY block above). Override with BACKFILL_SEED if you need a reproducible run.
# --tenant: the resolved tenant UUID, the value the dashboard binds into its ClickHouse read filter,
#         so seed data and the rendered tenant cannot drift (a NAME here matches no rows).
COMPOSE_NETWORK="${COMPOSE_NETWORK:-ai-tally_default}"
BACKFILL_SEED="${BACKFILL_SEED:-$(date +%s)}"
docker run --rm \
  --network "${COMPOSE_NETWORK}" \
  -v "${REPO_ROOT}/examples/vercel-chatbot/scripts:/scripts:ro" \
  -e TALLY_GATEWAY_URL="http://gateway:8080/v1/batches" \
  node:22-bookworm-slim \
  npx --yes tsx /scripts/backfill-spans.ts --seed "${BACKFILL_SEED}" --tenant "${TENANT_UUID}"

# Repair the web tier if it is running without TALLY_DEV_TENANT (for example after a bare
# `docker compose up` that bypassed deploy.sh). Compose recreates only when the value actually
# changes, so on a normal nightly run this is a no-op.
export TALLY_DEV_TENANT="${TENANT_UUID}"
"${COMPOSE[@]}" up -d web

echo "==> Demo data reset. The dashboard now shows a fresh synthetic 30-day window."
