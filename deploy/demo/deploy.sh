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

# Tenant-UUID resolution and the service-token preflight, shared with reseed.sh.
# shellcheck source=deploy/demo/lib-tenant.sh
source "${SCRIPT_DIR}/lib-tenant.sh"

# Cheap check first: a missing service token with auth on means the gateway never boots.
require_service_token_if_auth_on
warn_backfill_unsupported_if_auth_on

# CTO-367: pick the Caddy site file before anything starts. `basic` keeps the HTTP basic-auth block
# this kit has always had; `clerk` mounts a file without one, because Clerk's redirects and its
# organization.created webhook cannot pass a basic-auth challenge, and the webhook fails silently
# when they try: Clerk gets a 401, no tenant is provisioned, and the user waits on a workspace that
# never appears. Exported so Compose interpolates it into the caddy volume mount.
if [ "${AUTH_MODE:-basic}" = "clerk" ]; then
  export CADDY_SITE_FILE=Caddyfile.clerk
else
  export CADDY_SITE_FILE=Caddyfile
  : "${BASIC_AUTH_USER:?AUTH_MODE=basic needs BASIC_AUTH_USER in .env}"
  : "${BASIC_AUTH_HASH:?AUTH_MODE=basic needs BASIC_AUTH_HASH in .env (docker run --rm caddy:2 caddy hash-password --plaintext '...')}"
fi
echo "==> Building images and starting the stack (auth mode: ${AUTH_MODE:-basic})"
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
# seed: creates the `local-dev` tenant + API key + price catalog, and prints the tenant UUID.
# chatbot-demo-backfill: POSTs 30 days of backdated synthetic spans ($0, no LLM calls, no API keys).
echo "==> Seeding the demo tenant (make seed)"
make -C infra COMPOSE="docker compose --env-file ${REPO_ROOT}/${ENV_FILE} -f ${REPO_ROOT}/${BASE_COMPOSE} -f ${REPO_ROOT}/${PROD_COMPOSE}" seed

# --- Point the dashboard at the tenant that was just seeded -------------------------------------
# The UUID only exists after `make seed`, so the web container above started without it. Resolve it
# now and recreate just the web service with TALLY_DEV_TENANT set (plus the explicit no-auth opt-in
# this kit deliberately takes; see pin_dashboard_tenant in lib-tenant.sh, CTO-268).
echo "==> Resolving the demo tenant UUID"
TENANT_UUID="$(resolve_tenant_uuid)"
echo "    ${DEMO_TENANT_NAME} = ${TENANT_UUID}"

# CTO-367: two mutually exclusive modes, and the whole point is that they cannot overlap.
#
#   basic (default)  The synthetic-data demo this kit was written for. TALLY_DEV_TENANT pins the
#                    tenant, which turns the dashboard's own authentication off completely, and
#                    Caddy basic-auth is the only thing in front of it.
#   clerk            A real instance. TALLY_DEV_TENANT stays UNSET so Clerk resolves the tenant from
#                    the signed-in organization, and Caddy is TLS only.
#
# Setting TALLY_DEV_TENANT in clerk mode would silently disable auth on an instance the operator
# believes is protected, so the modes are exclusive here rather than additive.
if [ "${AUTH_MODE:-basic}" = "clerk" ]; then
  : "${NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY:?AUTH_MODE=clerk needs NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY in .env (Clerk dashboard, API Keys)}"
  : "${CLERK_SECRET_KEY:?AUTH_MODE=clerk needs CLERK_SECRET_KEY in .env (Clerk dashboard, API Keys)}"
  echo "==> Auth mode: CLERK. The dashboard requires sign-in; TALLY_DEV_TENANT stays unset."
  if [ -z "${CLERK_WEBHOOK_SIGNING_SECRET:-}" ]; then
    echo "    NOTE: CLERK_WEBHOOK_SIGNING_SECRET is unset. Sign-in will work, but organization.created"
    echo "          cannot be verified, so no tenant is ever provisioned and a new user lands on the"
    echo "          'setting up your workspace' screen forever. Add the webhook in Clerk once this"
    echo "          deployment has a URL, then re-run with the whsec_ value set."
  fi
  echo "    Demo tenant ${TENANT_UUID} exists and holds the seeded data. To put a Clerk"
  echo "    organization in front of it, run gateway.adopt_org after signing in (docs/runbook-tenants.md)."
else
  echo "==> Auth mode: BASIC. Pointing the dashboard at the demo tenant (dashboard auth OFF, Caddy basic-auth in front)"
  pin_dashboard_tenant "${TENANT_UUID}"
fi

echo "==> Backfilling 30 days of SYNTHETIC demo spans"
# The `make chatbot-demo-backfill` target runs on the HOST and POSTs to localhost:8080 - neither
# works on a locked-down single VM (no host Node, and the gateway publishes no host port in prod).
# The backfill script (examples/vercel-chatbot/scripts/backfill-spans.ts) imports only node:crypto,
# so we run it in a throwaway node container attached to the compose network, reaching the gateway
# internally as http://gateway:8080/v1/batches. Same generator, same $0 synthetic output.
# COMPOSE_NETWORK is `<project>_default`; the project name is `ai-tally` (infra/docker-compose.yml
# `name:`). Override COMPOSE_NETWORK in the environment if you renamed the project.
#
# Backfill under the SAME tenant UUID the dashboard was just pointed at. Both sides come from the
# one resolve_tenant_uuid call above, so the seeded data and the rendered tenant cannot drift into
# an empty dashboard (CTO-243).
#
# GATEWAY_SERVICE_TOKEN is threaded in because the backfill resolves its synthetic accounts'
# AccountIdHash through the control plane (POST /v1/tenant/account-lookup), which is service-token
# authenticated when TALLY_REQUIRE_API_KEY is on (Initiative 1 §6). Without it the backfill stops
# with a real error instead of shipping a corpus whose account dimension is empty.
COMPOSE_NETWORK="${COMPOSE_NETWORK:-ai-tally_default}"
docker run --rm \
  --network "${COMPOSE_NETWORK}" \
  -v "${REPO_ROOT}/examples/vercel-chatbot/scripts:/scripts:ro" \
  -e TALLY_GATEWAY_URL="http://gateway:8080/v1/batches" \
  -e GATEWAY_SERVICE_TOKEN="${TALLY_GATEWAY_SERVICE_TOKEN:-}" \
  node:22-bookworm-slim \
  npx --yes tsx /scripts/backfill-spans.ts --tenant "${TENANT_UUID}"

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
