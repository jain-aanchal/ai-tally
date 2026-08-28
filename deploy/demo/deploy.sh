#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# ai-tally demo-deploy-kit - one-shot deploy for a single VM behind Caddy (CTO-243).
#
# Brings up the whole stack (ClickHouse, Postgres, Redpanda, MinIO, gateway, web, Caddy), applies
# the ClickHouse DDL, and loads the SYNTHETIC demo dataset. Re-running is safe: compose reconciles
# to the desired state, the DDL is idempotent (CREATE ... IF NOT EXISTS), and seed/backfill are the
# same generators the local `make` targets use.
#
# Prereqs: Docker + Docker Compose v2, a filled-in deploy/demo/.env, and DNS for $DOMAIN pointed at
# this host (needed for Caddy to obtain a TLS cert, not for the containers to start).
#
# Usage:  ./deploy/demo/deploy.sh        (run from the repo root or anywhere; it finds the root)

set -euo pipefail

# --- Locate the repo root so this script works from any CWD -------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

ENV_FILE="deploy/demo/.env"
BASE_COMPOSE="infra/docker-compose.yml"
PROD_COMPOSE="deploy/demo/docker-compose.prod.yml"

# --- Load .env ----------------------------------------------------------------------------------
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found. Copy deploy/demo/.env.example to ${ENV_FILE} and fill it in." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${DOMAIN:?DOMAIN must be set in ${ENV_FILE}}"
: "${BASIC_AUTH_USER:?BASIC_AUTH_USER must be set in ${ENV_FILE}}"
: "${BASIC_AUTH_HASH:?BASIC_AUTH_HASH must be set in ${ENV_FILE} (see .env.example for the generator)}"

# Compose reads the same .env for base-stack defaults; pass it explicitly so both files see it.
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${BASE_COMPOSE}" -f "${PROD_COMPOSE}")

echo "==> Building images and starting the stack"
"${COMPOSE[@]}" up -d --build

# --- Wait for the gateway to be healthy before seeding ------------------------------------------
echo "==> Waiting for the gateway to become healthy"
for i in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T gateway \
      python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz').status==200 else 1)" \
      >/dev/null 2>&1; then
    echo "    gateway healthy"
    break
  fi
  if [[ "${i}" -eq 60 ]]; then
    echo "ERROR: gateway did not become healthy in time. Check: ${COMPOSE[*]} logs gateway" >&2
    exit 1
  fi
  sleep 2
done

# --- Apply ClickHouse DDL (incl. replay_samples) to the running stack ---------------------------
# initdb mounts only fire on a first boot against an empty volume, so replay the idempotent DDL.
echo "==> Applying ClickHouse DDL (make ch-migrate)"
make -C infra COMPOSE="docker compose --env-file ${REPO_ROOT}/${ENV_FILE} -f ${REPO_ROOT}/${BASE_COMPOSE} -f ${REPO_ROOT}/${PROD_COMPOSE}" ch-migrate

# --- Load the SYNTHETIC demo dataset ------------------------------------------------------------
# seed: creates the local-dev tenant + API key + price catalog.
# chatbot-demo-backfill: POSTs 30 days of backdated synthetic spans ($0, no LLM calls, no API keys).
echo "==> Seeding the demo tenant (make seed)"
make -C infra COMPOSE="docker compose --env-file ${REPO_ROOT}/${ENV_FILE} -f ${REPO_ROOT}/${BASE_COMPOSE} -f ${REPO_ROOT}/${PROD_COMPOSE}" seed

echo "==> Backfilling 30 days of SYNTHETIC demo spans"
# The `make chatbot-demo-backfill` target runs on the HOST and POSTs to localhost:8080 - neither
# works on a locked-down single VM (no host Node, and the gateway publishes no host port in prod).
# The backfill script (examples/vercel-chatbot/scripts/backfill-spans.ts) imports only node:crypto,
# so we run it in a throwaway node container attached to the compose network, reaching the gateway
# internally as http://gateway:8080/v1/batches. Same generator, same $0 synthetic output.
# COMPOSE_NETWORK is `<project>_default`; the project name is `ai-tally` (infra/docker-compose.yml
# `name:`). Override COMPOSE_NETWORK in the environment if you renamed the project.
#
# Backfill under the SAME tenant the dashboard renders (TALLY_TENANT_ID). `gateway.seed` creates the
# demo tenant as `local-dev`, so this is pinned to local-dev in .env.example; passing it here keeps
# the seeded data and the dashboard's tenant from drifting into an empty dashboard (CTO-243).
COMPOSE_NETWORK="${COMPOSE_NETWORK:-ai-tally_default}"
docker run --rm \
  --network "${COMPOSE_NETWORK}" \
  -v "${REPO_ROOT}/examples/vercel-chatbot/scripts:/scripts:ro" \
  -e TALLY_GATEWAY_URL="http://gateway:8080/v1/batches" \
  node:22-bookworm-slim \
  npx --yes tsx /scripts/backfill-spans.ts --tenant "${TALLY_TENANT_ID:-local-dev}"

# --- Done ---------------------------------------------------------------------------------------
cat <<EOF

==================================================================
  ai-tally demo is up.

  URL:   https://${DOMAIN}
  Login: ${BASIC_AUTH_USER}  (password: the plaintext you hashed into BASIC_AUTH_HASH)

  The dataset is SYNTHETIC (seeded + backfilled), safe to share with testers.
  Share the link and password privately. Reset the data with deploy/demo/reseed.sh.
==================================================================
EOF
