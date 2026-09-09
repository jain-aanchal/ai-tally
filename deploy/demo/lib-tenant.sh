#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# ai-tally demo-deploy-kit - shared preflight helpers for deploy.sh and reseed.sh (CTO-243).
#
# WHY this file exists: both scripts need the SAME tenant-UUID resolution and the SAME service-token
# preflight, and a copy-paste divergence between them is exactly the failure mode that produces a
# blank demo dashboard. Keeping one definition means deploy and the nightly reseed cannot drift.
#
# Sourced, never executed. The caller must already have sourced deploy/demo/.env and populated the
# COMPOSE array, e.g.
#
#   COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${BASE_COMPOSE}" -f "${PROD_COMPOSE}")
#   source deploy/demo/lib-tenant.sh

# The tenant NAME `gateway.seed` creates. Only the name is knowable ahead of time; the UUID is
# generated at seed time, which is why it has to be read back out of Postgres.
DEMO_TENANT_NAME="${DEMO_TENANT_NAME:-local-dev}"

# Resolve the demo tenant's UUID from the control plane, printing it on stdout.
#
# WHY the UUID and not the name (Initiative 1, §8): the canonical TenantId is the tenant UUID. The
# backfill tags every span with whatever `--tenant` it is handed, and the dashboard binds
# TALLY_DEV_TENANT straight into the ClickHouse read filter (`TenantId = ...`). Feed a NAME to
# either side and it matches no rows: the stack comes up green and the dashboard renders empty with
# no error anywhere. Mirrors the TENANT_UUID recipe in infra/Makefile.
#
# Honest under uncertainty: on any failure this returns non-zero with a real reason instead of
# falling back to `local-dev` or to an empty string, both of which would silently produce that empty
# dashboard.
resolve_tenant_uuid() {
  # The name is interpolated into SQL, so refuse anything that is not a plain identifier rather than
  # hand an operator typo (or worse) to psql.
  if [[ ! "${DEMO_TENANT_NAME}" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "ERROR: DEMO_TENANT_NAME='${DEMO_TENANT_NAME}' is not a plain identifier ([A-Za-z0-9_-]+)." >&2
    return 1
  fi

  local uuid
  uuid="$("${COMPOSE[@]}" exec -T postgres \
    psql -U "${POSTGRES_USER:-tally}" -d "${POSTGRES_DB:-tally}" -tAc \
    "SELECT id FROM tenants WHERE name='${DEMO_TENANT_NAME}' LIMIT 1" 2>/dev/null \
    | tr -d '[:space:]')"

  if [[ ! "${uuid}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    cat >&2 <<ERR
ERROR: could not resolve the '${DEMO_TENANT_NAME}' tenant UUID from Postgres.
       Got: '${uuid:-<empty>}'

       The dashboard reads spans by tenant UUID, so there is no safe fallback here: continuing
       with the tenant NAME (or with nothing) would leave you with a green stack and an empty
       dashboard. Fix the cause and re-run.

       Check, in order:
         1. Postgres is up:   ${COMPOSE[*]} ps postgres
         2. The tenant exists: ${COMPOSE[*]} exec -T postgres \\
              psql -U ${POSTGRES_USER:-tally} -d ${POSTGRES_DB:-tally} -c 'SELECT id, name FROM tenants'
         3. If it is missing, the seed step did not run or failed:
              make -C infra seed
ERR
    return 1
  fi

  printf '%s\n' "${uuid}"
}

# Export the two variables the demo's web tier needs, and recreate the web service with them.
#
# WHY TWO (CTO-268): TALLY_DEV_TENANT pins the tenant AND turns the dashboard's authentication off
# entirely (no Clerk middleware, no ClerkProvider, every visitor an org admin). That is a deliberate
# choice for this kit, which serves SYNTHETIC data behind Caddy basic auth on its own host. It is a
# disaster on a real instance, and this kit is the thing people copy. So the web tier now REFUSES TO
# BOOT on TALLY_DEV_TENANT alone in a production build, and serving with no auth takes a second,
# unmistakable variable that nobody sets by accident. The demo says it out loud, here, once, so both
# scripts agree and so an operator reading this file sees exactly what the demo is opting into.
#
# Takes the resolved tenant UUID. Exported (not passed in --env-file) so Compose interpolation picks
# them up: a host env var wins over the same key in the env file.
pin_dashboard_tenant() {
  local tenant_uuid="$1"
  export TALLY_DEV_TENANT="${tenant_uuid}"
  export TALLY_ALLOW_INSECURE_NO_AUTH=1
  "${COMPOSE[@]}" up -d web
}

# Fail before the stack is touched when gateway auth is on but no service token was supplied.
#
# WHY up front (Initiative 1, §6): with TALLY_REQUIRE_API_KEY on and TALLY_GATEWAY_SERVICE_TOKEN
# empty, the gateway deliberately REFUSES TO BOOT rather than expose an unauthenticated control
# plane. Without this check that surfaces two minutes later as an opaque health-check timeout.
require_service_token_if_auth_on() {
  local auth_on="${TALLY_REQUIRE_API_KEY:-false}"
  case "${auth_on}" in
    true|TRUE|True|1|yes|on) ;;
    *) return 0 ;;
  esac

  if [[ -z "${TALLY_GATEWAY_SERVICE_TOKEN:-}" ]]; then
    cat >&2 <<ERR
ERROR: TALLY_REQUIRE_API_KEY is on but TALLY_GATEWAY_SERVICE_TOKEN is empty.

       The gateway refuses to boot in that state rather than come up with an open control plane,
       and every dashboard control-plane write would fail. Generate a real token and put it in
       deploy/demo/.env:

         echo "TALLY_GATEWAY_SERVICE_TOKEN=\$(openssl rand -hex 32)" >> deploy/demo/.env

       The same value reaches the gateway as TALLY_GATEWAY_SERVICE_TOKEN and the web tier as
       GATEWAY_SERVICE_TOKEN (see deploy/demo/docker-compose.prod.yml); they must match.
ERR
    return 1
  fi
}

# Say up front that this kit's SEEDING path does not work with gateway auth on.
#
# WHY (CTO-243): with TALLY_REQUIRE_API_KEY on, /v1/batches demands a bearer ingest API key, but
# examples/vercel-chatbot/scripts/backfill-spans.ts sends no Authorization header and has no
# api-key option, so it takes a 401 on the first batch. Without this warning the operator finds out
# minutes into a run that has already built images and seeded, which is exactly the late failure
# the preflight exists to eliminate. This warns rather than aborts because bringing the stack up
# with auth on is still a valid thing to do; only the synthetic backfill cannot run that way.
warn_backfill_unsupported_if_auth_on() {
  local auth_on="${TALLY_REQUIRE_API_KEY:-false}"
  case "${auth_on}" in
    true|TRUE|True|1|yes|on) ;;
    *) return 0 ;;
  esac

  cat >&2 <<'WARN'
WARNING: TALLY_REQUIRE_API_KEY is on, and the demo kit's SEEDING path does not support that.

         The synthetic backfill (examples/vercel-chatbot/scripts/backfill-spans.ts) posts to
         /v1/batches with no Authorization header, and with auth on the gateway answers 401. The
         stack and the dashboard will come up fine; the backfill step near the end of this run WILL
         fail and the dashboard will have no demo data.

         To seed the demo, run with auth off (leave TALLY_REQUIRE_API_KEY unset or false), then
         turn auth on afterwards if you need it. Continuing anyway.
WARN
}
