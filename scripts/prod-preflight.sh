#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Production deployment preflight (CTO-360). Run it BEFORE deploying anything.
#
# WHY THIS IS A SHELL SCRIPT AND NOT A PYTHON MODULE OR A TEST. The person running it has just
# filled in a Vercel project and an ECS task definition and has not necessarily got a repo checkout,
# a uv venv, node_modules, docker, or AWS credentials. The only things it may assume are bash,
# coreutils and (for the optional reachability probe) curl. It also has to see BOTH sides of the
# deployment at once, because the single most confusing failure here is a service token that differs
# between the web tier and the gateway, and no check that runs inside one of them can catch that.
#
# WHAT IT DELIBERATELY DOES NOT DO:
#   * It needs no AWS credentials and makes no AWS API call.
#   * It never prints a secret. Secrets are compared and reported by SHA-256 prefix and length.
#   * It does not connect to Postgres or authenticate to anything. The optional probe is an
#     unauthenticated GET whose only question is "does this host answer at all".
#
# Every check names the SYMPTOM it prevents, because these fail in confusing and unrelated-looking
# ways: a silently-open control plane, a dashboard that 401s everywhere, a sign-in that never
# resolves a tenant, and a dashboard that paints mock numbers at a real customer.

set -uo pipefail

FAILURES=0
WARNINGS=0
PROBE=1
ENV_FILES=()

usage() {
  cat <<'USAGE'
Usage: scripts/prod-preflight.sh [--env FILE]... [--no-probe]

Validates the deployment configuration for a production ai-tally before anything is deployed.
Reads settings from the current environment, plus any --env dotenv files (repeatable, later files
win). Pass BOTH sides: the web tier's Vercel settings and the gateway's settings. They use
different variable names, so one merged environment is unambiguous.

  --env FILE    A dotenv-format file (KEY=value per line). Values are read, never executed.
  --no-probe    Skip the outbound HTTPS reachability probe (offline checks still run).
  -h, --help    This text.

Exits non-zero if any check fails. Never prints a secret value.

Variables it looks at
  gateway:  TALLY_ENV  TALLY_REQUIRE_API_KEY  TALLY_GATEWAY_SERVICE_TOKEN
            TALLY_HMAC_KEY_PROVIDER  TALLY_ALLOW_INSECURE_NO_AUTH
  web:      TALLY_GATEWAY_URL  GATEWAY_SERVICE_TOKEN  TALLY_CLICKHOUSE_URL  TALLY_DEV_TENANT
            CLERK_WEBHOOK_SIGNING_SECRET  NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY  CLERK_SECRET_KEY
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --env) [ $# -ge 2 ] || { echo "--env needs a file" >&2; exit 2; }; ENV_FILES+=("$2"); shift 2 ;;
    --env=*) ENV_FILES+=("${1#--env=}"); shift ;;
    --no-probe) PROBE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Dotenv loading, by parsing rather than by sourcing. Sourcing an operator's env file executes
# whatever is in it, and these files hold production credentials pasted from a console; a stray
# backtick should not run a command.
load_env_file() {
  local file="$1" line key value
  if [ ! -r "$file" ]; then
    echo "cannot read env file: $file" >&2
    exit 2
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"          # ltrim
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    case "$line" in *=*) : ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in [A-Za-z_]*) : ;; *) continue ;; esac
    value="${value%"${value##*[![:space:]]}"}"       # rtrim
    # Strip one layer of matching quotes, which is how a console-pasted value usually arrives.
    case "$value" in
      \"*\") value="${value%\"}"; value="${value#\"}" ;;
      \'*\') value="${value%\'}"; value="${value#\'}" ;;
    esac
    export "$key=$value"
  done < "$file"
}

for f in ${ENV_FILES+"${ENV_FILES[@]}"}; do
  load_env_file "$f"
  echo "loaded $f"
done

# --- reporting -----------------------------------------------------------------------------------

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

fail() {
  FAILURES=$((FAILURES + 1))
  printf '  \033[31mFAIL\033[0m  %s\n' "$1"
  printf '        symptom if shipped: %s\n' "$2"
  printf '        fix:                %s\n' "$3"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  printf '  \033[33mWARN\033[0m  %s\n' "$1"
  printf '        symptom if shipped: %s\n' "$2"
  printf '        fix:                %s\n' "$3"
}

# Secret fingerprint: enough to compare two values and to tell them apart in a transcript, and not
# enough to reconstruct either. Never the value itself.
fingerprint() {
  local value="$1" digest=""
  if command -v shasum >/dev/null 2>&1; then
    digest=$(printf '%s' "$value" | shasum -a 256 | cut -d' ' -f1)
  elif command -v sha256sum >/dev/null 2>&1; then
    digest=$(printf '%s' "$value" | sha256sum | cut -d' ' -f1)
  elif command -v openssl >/dev/null 2>&1; then
    digest=$(printf '%s' "$value" | openssl dgst -sha256 | awk '{print $NF}')
  else
    printf 'len=%d sha256=unavailable' "${#value}"
    return
  fi
  printf 'len=%d sha256=%s...' "${#value}" "${digest:0:8}"
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

echo
echo "ai-tally production preflight"
echo "no AWS credentials are used, and no secret value is printed"
echo

# --- 1. the control plane is actually gated ------------------------------------------------------

echo "1. control-plane authentication"
require_api_key=$(lower "${TALLY_REQUIRE_API_KEY:-}")
case "$require_api_key" in
  true|1|yes|on)
    pass "TALLY_REQUIRE_API_KEY is ${TALLY_REQUIRE_API_KEY}"
    ;;
  "")
    fail "TALLY_REQUIRE_API_KEY is unset (the gateway default is false)" \
         "the control-plane service-token gate is not enforced at all, so anyone who can reach the gateway can POST /v1/tenant/provision and create a real tenant, unauthenticated" \
         "set TALLY_REQUIRE_API_KEY=true on the gateway"
    ;;
  *)
    fail "TALLY_REQUIRE_API_KEY is ${TALLY_REQUIRE_API_KEY}" \
         "the control-plane service-token gate is not enforced at all, so anyone who can reach the gateway can POST /v1/tenant/provision and create a real tenant, unauthenticated" \
         "set TALLY_REQUIRE_API_KEY=true on the gateway"
    ;;
esac

# TALLY_ENV is the backstop that makes the setting above LOUD instead of silent: with it set the
# gateway refuses to boot when authentication is off (app.py assert_auth_config). Without it the
# dangerous combination just serves.
if [ "$(lower "${TALLY_ENV:-}")" = "production" ]; then
  pass "TALLY_ENV=production (the gateway refuses to boot with authentication off)"
else
  fail "TALLY_ENV is '${TALLY_ENV:-<unset>}', not production" \
       "the gateway's boot guard is inactive, so a later edit turning TALLY_REQUIRE_API_KEY off starts a wide-open control plane silently instead of refusing to start" \
       "set TALLY_ENV=production on the gateway task"
fi

if [ -n "${TALLY_ALLOW_INSECURE_NO_AUTH:-}" ]; then
  fail "TALLY_ALLOW_INSECURE_NO_AUTH is set" \
       "it is the explicit opt-out of both boot guards, web and gateway; with it set, a production build serves with authentication off instead of refusing to start" \
       "unset TALLY_ALLOW_INSECURE_NO_AUTH everywhere except the synthetic-data demo kit"
else
  pass "TALLY_ALLOW_INSECURE_NO_AUTH is unset"
fi

# --- 2. the two halves of the service token agree ------------------------------------------------

echo
echo "2. control-plane service token (web <-> gateway)"
web_token="${GATEWAY_SERVICE_TOKEN:-}"
gw_token="${TALLY_GATEWAY_SERVICE_TOKEN:-}"

if [ -z "$web_token" ] && [ -z "$gw_token" ]; then
  fail "neither GATEWAY_SERVICE_TOKEN (web) nor TALLY_GATEWAY_SERVICE_TOKEN (gateway) is set" \
       "with TALLY_REQUIRE_API_KEY=true the gateway refuses to boot; with it false the control plane is unauthenticated" \
       "generate one value with 'openssl rand -hex 32' and set it in BOTH places"
elif [ -z "$web_token" ]; then
  fail "GATEWAY_SERVICE_TOKEN (web, Vercel) is empty; the gateway has one ($(fingerprint "$gw_token"))" \
       "the dashboard sends no Authorization header, so every control-plane call 401s and a signed-in user sees a broken dashboard with nothing naming the cause" \
       "set GATEWAY_SERVICE_TOKEN in the Vercel project to the gateway's TALLY_GATEWAY_SERVICE_TOKEN"
elif [ -z "$gw_token" ]; then
  fail "TALLY_GATEWAY_SERVICE_TOKEN (gateway) is empty; the web tier has one ($(fingerprint "$web_token"))" \
       "the gateway refuses to boot when TALLY_REQUIRE_API_KEY is true and no token is configured" \
       "set TALLY_GATEWAY_SERVICE_TOKEN on the gateway to the same value the Vercel project holds"
elif [ "$web_token" = "$gw_token" ]; then
  pass "GATEWAY_SERVICE_TOKEN == TALLY_GATEWAY_SERVICE_TOKEN ($(fingerprint "$gw_token"))"
else
  fail "the two service tokens differ: web $(fingerprint "$web_token"), gateway $(fingerprint "$gw_token")" \
       "every control-plane call 401s, which surfaces to a signed-in user as a broken dashboard with no obvious cause" \
       "copy one value into both. A trailing newline or a quote pasted from a console counts as a difference"
fi

# --- 3. no dev escape hatch ----------------------------------------------------------------------

echo
echo "3. dev escape hatch"
if [ -n "${TALLY_DEV_TENANT:-}" ]; then
  fail "TALLY_DEV_TENANT is set" \
       "it pins one tenant and short-circuits Clerk, so the dashboard serves that tenant's data to whoever loads it. A production build already refuses to boot with it set unless TALLY_ALLOW_INSECURE_NO_AUTH is also set (PR #345); this catches it before the deploy rather than during it" \
       "remove TALLY_DEV_TENANT from the Vercel project. It is for keyless local dev and CI only"
else
  pass "TALLY_DEV_TENANT is unset (Clerk resolves the tenant)"
fi

# --- 4. HMAC key material is held by reference ---------------------------------------------------

echo
echo "4. per-tenant HMAC key provider"
hmac_provider="$(lower "${TALLY_HMAC_KEY_PROVIDER:-}")"
case "$hmac_provider" in
  "")
    fail "TALLY_HMAC_KEY_PROVIDER is unset (the gateway default is 'local')" \
         "per-tenant HMAC key material is then derived from a root secret in configuration rather than held by reference, against the identifiers-by-hash / credentials-by-reference invariant" \
         "set TALLY_HMAC_KEY_PROVIDER=kms on AWS (Secrets Manager), or set it to 'local' explicitly if this is a single-tenant instance and you mean it"
    ;;
  local)
    warn "TALLY_HMAC_KEY_PROVIDER=local, set explicitly" \
         "the root secret in configuration IS the key material for every tenant, so a config leak is a leak of every tenant's hashing key. Acceptable only on a single-tenant instance" \
         "use TALLY_HMAC_KEY_PROVIDER=kms on any instance that will hold more than one tenant"
    ;;
  *)
    pass "TALLY_HMAC_KEY_PROVIDER=${TALLY_HMAC_KEY_PROVIDER}"
    ;;
esac

# --- 5. Clerk can actually provision a tenant ----------------------------------------------------

echo
echo "5. Clerk"
if [ -z "${CLERK_WEBHOOK_SIGNING_SECRET:-}" ]; then
  fail "CLERK_WEBHOOK_SIGNING_SECRET is unset" \
       "the svix signature on organization.created cannot be verified, so the webhook is rejected and no tenant is ever provisioned. It presents as sign-in succeeding and then failing to resolve a tenant, which looks like a dashboard bug rather than a missing secret" \
       "copy the signing secret from the Clerk webhook endpoint (it starts whsec_) into the Vercel project"
elif [ "${CLERK_WEBHOOK_SIGNING_SECRET:0:6}" != "whsec_" ]; then
  warn "CLERK_WEBHOOK_SIGNING_SECRET does not start with whsec_ ($(fingerprint "${CLERK_WEBHOOK_SIGNING_SECRET}"))" \
       "if this is the API key rather than the endpoint's signing secret, every webhook fails signature verification and no tenant is provisioned" \
       "take the value from the webhook ENDPOINT in the Clerk dashboard, not from the API keys page"
else
  pass "CLERK_WEBHOOK_SIGNING_SECRET is set ($(fingerprint "${CLERK_WEBHOOK_SIGNING_SECRET}"))"
fi

for var in NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY CLERK_SECRET_KEY; do
  if [ -z "${!var:-}" ]; then
    fail "$var is unset" \
         "nobody can sign in at all, and the publishable key in particular is inlined into the client bundle at BUILD time, so setting it later needs a rebuild rather than a restart" \
         "set $var in the Vercel project before the build"
  else
    pass "$var is set ($(fingerprint "${!var}"))"
  fi
done

# --- 6. URLs are public HTTPS --------------------------------------------------------------------

echo
echo "6. URLs reachable from Vercel"

check_url() {
  local var="$1" url="${2:-}" symptom="$3"
  if [ -z "$url" ]; then
    fail "$var is unset" "$symptom" "set $var in the Vercel project"
    return
  fi
  case "$url" in
    https://*) : ;;
    *)
      fail "$var is not https ($url)" "$symptom" \
           "Vercel functions egress to the public internet; use an https:// URL"
      return
      ;;
  esac
  local host="${url#https://}"; host="${host%%/*}"; host="${host%%:*}"
  case "$host" in
    localhost|127.*|0.0.0.0|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|*.internal|*.local)
      fail "$var points at a private or loopback host ($host)" "$symptom" \
           "Vercel functions run outside your VPC, so the host must resolve and be reachable publicly"
      return
      ;;
  esac
  pass "$var is public https ($host)"
}

check_url TALLY_GATEWAY_URL "${TALLY_GATEWAY_URL:-}" \
  "every control-plane call from the dashboard fails, so tenant resolution, settings and connector writes are all dead"

# This one is silent by design and that is exactly why it is here. tryLive() in web/lib/clickhouse.ts
# catches a ClickHouse query failure and returns null so the caller falls back to MOCK data. An
# unreachable ClickHouse therefore does not render an error; it renders somebody else's demo numbers
# in front of a real customer, which is what the honest-under-uncertainty invariant exists to forbid.
check_url TALLY_CLICKHOUSE_URL "${TALLY_CLICKHOUSE_URL:-}" \
  "the dashboard does NOT error. web/lib/clickhouse.ts tryLive() swallows the failure and falls back to MOCK data, so a real customer is shown fabricated numbers with no indication they are fake"

if [ "$PROBE" = "1" ] && command -v curl >/dev/null 2>&1; then
  probe() {
    local label="$1" url="$2" path="$3" symptom="$4"
    [ -n "$url" ] || return 0
    case "$url" in https://*) : ;; *) return 0 ;; esac
    local target="${url%/}$path" code
    # No credentials are sent. Any HTTP status means the host answered, which is the whole question;
    # only a transport failure (DNS, TLS, connect, timeout) is a finding.
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$target" 2>/dev/null)
    if [ -z "$code" ] || [ "$code" = "000" ]; then
      fail "$label did not answer at $target" "$symptom" \
           "check DNS, the certificate, and that the listener is public. Re-run with --no-probe if you are on a network that blocks egress"
    else
      pass "$label answered $target with HTTP $code"
    fi
  }
  probe "gateway" "${TALLY_GATEWAY_URL:-}" "/healthz" \
    "the dashboard cannot reach the gateway from Vercel, so tenant resolution fails for every signed-in user"
  probe "ClickHouse" "${TALLY_CLICKHOUSE_URL:-}" "/ping" \
    "the dashboard falls back to MOCK data rather than erroring, showing fabricated numbers to a real customer"
elif [ "$PROBE" = "1" ]; then
  warn "curl is not installed, so the reachability probe was skipped" \
       "an unreachable ClickHouse shows mock data instead of an error, and nothing offline can detect that" \
       "install curl and re-run, or check both URLs from outside your network by hand"
fi

# --- summary -------------------------------------------------------------------------------------

echo
echo "-------------------------------------------------------------------------------"
echo "What this CANNOT check: whether your AWS region offers Fargate ARM64 or your RDS"
echo "engine version and instance class (see deploy/aws/terraform/README.md), whether"
echo "the Clerk webhook endpoint URL points at this deployment, or whether the Postgres"
echo "schema has been migrated. A green run is a floor, not a guarantee."
echo

if [ "$FAILURES" -gt 0 ]; then
  printf '\033[31mPREFLIGHT FAILED\033[0m: %d failure(s), %d warning(s). Fix the FAIL lines above before deploying.\n' \
    "$FAILURES" "$WARNINGS"
  exit 1
fi

printf '\033[32mPREFLIGHT PASSED\033[0m: 0 failures, %d warning(s).\n' "$WARNINGS"
