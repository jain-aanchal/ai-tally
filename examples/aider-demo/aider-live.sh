#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run Aider INTERACTIVELY through the ai-tally edge proxy, then emit one batch
# per turn the model completed so the dashboard fills in as you work.
#
# Usage:   bash examples/aider-demo/aider-live.sh [PROVIDER=openai|anthropic] -- [aider args...]
# Example: bash examples/aider-demo/aider-live.sh PROVIDER=anthropic
#          bash examples/aider-demo/aider-live.sh -- string_utils.py test_string_utils.py
#
# Watch live updates at:   http://localhost:3000/agents?tag=aider-live
# (refresh after each Aider response — the page does not stream)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
PROVIDER="${PROVIDER:-openai}"
FEATURE_TAG="${FEATURE_TAG:-aider-live}"
TENANT="${TENANT:-local-dev}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
PROXY_PORT="${PROXY_PORT:-7070}"
PROXY_PID_FILE="${PROXY_PID_FILE:-/tmp/ai-tally-aider-edge-proxy.pid}"

# Allow "PROVIDER=anthropic --" prefix on the CLI
while [[ $# -gt 0 && "$1" != "--" ]]; do
  case "$1" in
    PROVIDER=*) PROVIDER="${1#PROVIDER=}";;
    FEATURE_TAG=*) FEATURE_TAG="${1#FEATURE_TAG=}";;
    *) break;;
  esac
  shift
done
[[ "${1:-}" == "--" ]] && shift

# 1. Validate the API key and pick a model.
if [[ "$PROVIDER" == "anthropic" ]]; then
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] || { echo "ERROR: ANTHROPIC_API_KEY not set"; exit 1; }
  AIDER_MODEL="${AIDER_MODEL:-anthropic/claude-sonnet-4-5}"
  upstream="https://api.anthropic.com"
  export ANTHROPIC_API_BASE="http://localhost:$PROXY_PORT"
  export ANTHROPIC_DEFAULT_HEADERS="X-Tally-Feature-Tag=$FEATURE_TAG,X-Tenant-Key=$TENANT"
else
  [[ -n "${OPENAI_API_KEY:-}" ]] || { echo "ERROR: OPENAI_API_KEY not set"; exit 1; }
  AIDER_MODEL="${AIDER_MODEL:-gpt-4o}"
  upstream="https://api.openai.com"
  export OPENAI_API_BASE="http://localhost:$PROXY_PORT/v1"
  export OPENAI_BASE_URL="http://localhost:$PROXY_PORT/v1"
  export OPENAI_DEFAULT_HEADERS="X-Tally-Feature-Tag=$FEATURE_TAG,X-Tenant-Key=$TENANT"
fi

# 2. Stack must be up.
if ! curl -sf "$GATEWAY_URL/healthz" >/dev/null 2>&1; then
  echo "ERROR: ai-tally gateway not reachable at $GATEWAY_URL"
  echo "       Run \`cd infra && make up && make seed\` first."
  exit 1
fi
if ! curl -sf http://localhost:3000 >/dev/null 2>&1; then
  echo "WARN:  dashboard not running at http://localhost:3000 — start it with:"
  echo "         cd web && npm run dev"
  echo "       (you can keep going; spans will land in ClickHouse either way)"
fi

# 3. Start the edge proxy if not already running.
if [[ -f "$PROXY_PID_FILE" ]] && kill -0 "$(cat "$PROXY_PID_FILE")" 2>/dev/null; then
  echo "  edge-proxy already running (pid $(cat "$PROXY_PID_FILE"))"
else
  echo "▶ starting edge-proxy on :$PROXY_PORT → $upstream"
  ( cd "$here/../../infra/edge-proxy" && \
    EDGE_PROXY_LISTEN=":$PROXY_PORT" \
    EDGE_PROXY_UPSTREAM="$upstream" \
    go run ./cmd/edge-proxy >/tmp/ai-tally-aider-edge-proxy.log 2>&1 ) &
  echo $! > "$PROXY_PID_FILE"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    nc -z localhost "$PROXY_PORT" 2>/dev/null && break
    sleep 0.5
  done
fi

# 4. Background tail: every second, scan the aider session log for new
#    "Cost: $X message" lines and emit a feature-tagged batch per finding.
log=$(mktemp)
emitted_lines=0
emit_loop() {
  while sleep 1; do
    [[ -f "$log" ]] || continue
    current=$(grep -cE 'Cost: \$[0-9.]+ message' "$log" 2>/dev/null || true)
    current=${current:-0}
    if [[ "$current" -gt "$emitted_lines" ]]; then
      # Emit one batch for each new turn we haven't seen yet.
      while [[ "$emitted_lines" -lt "$current" ]]; do
        emitted_lines=$((emitted_lines + 1))
        cost=$(grep -oE 'Cost: \$[0-9.]+ message' "$log" | sed -n "${emitted_lines}p" | grep -oE '[0-9.]+' || true)
        cost=${cost:-0}
        trace_id=$(python3 -c "import uuid;print(uuid.uuid4().hex)")
        python3 "$here/_emit_batch.py" \
          --gateway "$GATEWAY_URL/v1/batches" \
          --tenant "$TENANT" \
          --feature-tag "$FEATURE_TAG" \
          --trace-id "$trace_id" \
          --cost-usd "$cost" \
          --turns 1 \
          --provider "$PROVIDER" >/dev/null 2>&1 || true
        echo "  ▸ emitted turn $emitted_lines (\$$cost) → http://localhost:3000/agents?tag=$FEATURE_TAG" >&2
      done
    fi
  done
}
emit_loop &
emit_pid=$!
trap "kill $emit_pid 2>/dev/null || true; rm -f $log" EXIT

echo
echo "─── aider-live: ai-tally edge-proxy + emit loop ready ────────────"
echo "  feature.tag = $FEATURE_TAG    model = $AIDER_MODEL"
echo "  dashboard:    http://localhost:3000/agents?tag=$FEATURE_TAG"
echo "  refresh that tab after each Aider response to see live updates."
echo "─────────────────────────────────────────────────────────────────"
echo

# 5. Run Aider interactively (no redirection of stdin — full TTY).
#    Tee its output to $log so the emit loop can parse "Cost: $X message" lines.
aider --no-git --yes --model "$AIDER_MODEL" "$@" 2>&1 | tee "$log"
