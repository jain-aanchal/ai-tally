#!/usr/bin/env bash
# Boots the vendored Vercel AI Chatbot on :3001 (if not already running) and
# drives 50 synthetic chat sessions through it into the local ai-tally
# gateway. Scripted sessions — not real users.
# Usage:  bash examples/vercel-chatbot/run.sh [-- --sessions N --conversion-rate 0..1 ...]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${HERE}/app"
PID_FILE="${TALLY_CHATBOT_PID_FILE:-/tmp/ai-tally-chatbot.pid}"
LOG_FILE="${TALLY_CHATBOT_LOG_FILE:-/tmp/ai-tally-chatbot.log}"
GATEWAY_URL="${TALLY_GATEWAY_URL:-http://localhost:8080}"
CHATBOT_URL="${TALLY_CHATBOT_URL:-http://localhost:3001}"

# shellcheck source=examples/vercel-chatbot/_dashboard-links.sh
. "${HERE}/_dashboard-links.sh"

if [ -f "${HERE}/.env" ]; then
  set -a; . "${HERE}/.env"; set +a
fi

err() { echo "✗ $*" >&2; exit 1; }

[ -n "${OPENAI_API_KEY:-}" ]    || err "OPENAI_API_KEY is required (export it or put it in ${HERE}/.env)"
[ -n "${ANTHROPIC_API_KEY:-}" ] || err "ANTHROPIC_API_KEY is required (export it or put it in ${HERE}/.env)"

GATEWAY_HEALTH="${GATEWAY_URL%/}/healthz"
if ! curl -fsS --max-time 3 "${GATEWAY_HEALTH}" >/dev/null; then
  err "ai-tally gateway not reachable at ${GATEWAY_HEALTH}. Run 'make up' from infra/ first."
fi

if curl -fsS --max-time 2 "${CHATBOT_URL}/api/demo-chat" -X POST \
    -H 'content-type: application/json' -d '{"sessionId":"probe","prompt":"hi","provider":"openai","dryRun":true}' \
    >/dev/null 2>&1; then
  echo "✓ Chatbot already up at ${CHATBOT_URL}"
else
  echo "→ Booting chatbot on ${CHATBOT_URL}…"
  if [ ! -d "${APP_DIR}/node_modules" ]; then
    echo "  · installing chatbot deps (pnpm install)…"
    (cd "${APP_DIR}" && pnpm install --silent --prefer-offline) || \
      (cd "${APP_DIR}" && npm install --silent --no-audit --no-fund)
  fi
  TALLY_GATEWAY_URL="${TALLY_GATEWAY_URL:-${GATEWAY_URL}/v1/batches}" \
  TALLY_TENANT="${TALLY_TENANT:-local-dev}" \
    nohup pnpm --dir "${APP_DIR}" exec next dev --turbo --port 3001 \
      >"${LOG_FILE}" 2>&1 &
  echo $! > "${PID_FILE}"
  for _ in $(seq 1 30); do
    sleep 1
    if curl -fsS --max-time 1 "${CHATBOT_URL}" >/dev/null 2>&1; then break; fi
  done
  curl -fsS --max-time 2 "${CHATBOT_URL}" >/dev/null 2>&1 || \
    err "Chatbot did not come up — see ${LOG_FILE}"
  echo "✓ Chatbot up (pid $(cat "${PID_FILE}"), log ${LOG_FILE})"
fi

echo "→ Driving synthetic traffic…"
DRIVER_ARGS=()
if [ $# -gt 0 ] && [ "$1" = "--" ]; then shift; DRIVER_ARGS=("$@"); fi
(cd "${APP_DIR}" && pnpm --silent exec tsx "${HERE}/scripts/drive-traffic.ts" "${DRIVER_ARGS[@]}")

echo ""
print_links
open_workflow_4
