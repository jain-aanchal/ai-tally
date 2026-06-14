#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# make aider-demo entry point: run Aider against the fixture repo through the
# ai-tally edge proxy, then deep-link the dashboard to the just-recorded traces.
# Self-contained: see README.md for prereqs ("`make up && make seed` first").
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./_dashboard-links.sh
source "$here/_dashboard-links.sh"

PROVIDER="${PROVIDER:-openai}"
FEATURE_TAG="${FEATURE_TAG:-aider-demo}"
TENANT="${TENANT:-local-dev}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
PROXY_PORT="${PROXY_PORT:-7070}"
PROXY_PID_FILE="${PROXY_PID_FILE:-/tmp/ai-tally-aider-edge-proxy.pid}"

# 1. Validate the API key for the chosen provider.
if [[ "$PROVIDER" == "anthropic" ]]; then
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] || { echo "ERROR: ANTHROPIC_API_KEY not set"; exit 1; }
  upstream="https://api.anthropic.com"
else
  [[ -n "${OPENAI_API_KEY:-}" ]] || { echo "ERROR: OPENAI_API_KEY not set (or pass PROVIDER=anthropic)"; exit 1; }
  upstream="https://api.openai.com"
fi

# 2. Stack must be up — fail with a helpful pointer otherwise.
if ! curl -sf "$GATEWAY_URL/healthz" >/dev/null 2>&1; then
  echo "ERROR: ai-tally gateway not reachable at $GATEWAY_URL"
  echo "       Run \`cd infra && make up && make seed\` first."
  exit 1
fi

# 3. Start the edge proxy on $PROXY_PORT if not already running.
start_proxy() {
  if [[ -f "$PROXY_PID_FILE" ]] && kill -0 "$(cat "$PROXY_PID_FILE")" 2>/dev/null; then
    echo "  edge-proxy already running (pid $(cat "$PROXY_PID_FILE"))"; return
  fi
  echo "▶ starting edge-proxy on :$PROXY_PORT → $upstream"
  ( cd "$here/../../infra/edge-proxy" && \
    EDGE_PROXY_LISTEN=":$PROXY_PORT" \
    EDGE_PROXY_UPSTREAM="$upstream" \
    go run ./cmd/edge-proxy >/tmp/ai-tally-aider-edge-proxy.log 2>&1 ) &
  echo $! > "$PROXY_PID_FILE"
  # Wait up to ~5s for it to bind.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://localhost:$PROXY_PORT" -o /dev/null 2>/dev/null || \
       nc -z localhost "$PROXY_PORT" 2>/dev/null; then return; fi
    sleep 0.5
  done
  echo "ERROR: edge-proxy didn't bind on :$PROXY_PORT (see /tmp/ai-tally-aider-edge-proxy.log)"; exit 1
}
start_proxy

# 4. Point Aider at the proxy and pick a model that matches the provider.
#    Aider speaks OpenAI protocol by default; for Anthropic we explicitly select
#    a Claude model so it speaks Anthropic protocol to the proxy upstream.
if [[ "$PROVIDER" == "anthropic" ]]; then
  # Aider uses LiteLLM under the hood; LiteLLM requires the provider prefix.
  AIDER_MODEL="${AIDER_MODEL:-anthropic/claude-sonnet-4-5}"
  export ANTHROPIC_API_BASE="http://localhost:$PROXY_PORT"
  export ANTHROPIC_DEFAULT_HEADERS="X-Tally-Feature-Tag=$FEATURE_TAG,X-Tenant-Key=$TENANT"
else
  AIDER_MODEL="${AIDER_MODEL:-gpt-4o}"
  export OPENAI_API_BASE="http://localhost:$PROXY_PORT/v1"
  export OPENAI_BASE_URL="http://localhost:$PROXY_PORT/v1"
  export OPENAI_DEFAULT_HEADERS="X-Tally-Feature-Tag=$FEATURE_TAG,X-Tenant-Key=$TENANT"
fi

# 5. Run each task. Parses Aider's "Tokens: …" cost line for the per-task summary.
declare -a trace_ids=()
total_cost_usd=0
total_turns=0
i=0
for task_file in "$here"/tasks/*.txt; do
  i=$((i+1))
  total=$(ls "$here"/tasks/*.txt | wc -l | tr -d ' ')
  first_line=$(head -n 1 "$task_file")
  printf "▶ task %d/%d: %s\n" "$i" "$total" "${first_line:0:60}…"

  start=$(date +%s)
  log=$(mktemp)
  if ! command -v aider >/dev/null 2>&1; then
    echo "ERROR: aider not on PATH. Install with: pip install aider-chat"; exit 1
  fi
  ( cd "$here/target-repo" && \
    aider --no-git --yes --no-stream --model "$AIDER_MODEL" \
          --message-file "$task_file" \
          string_utils.py test_string_utils.py >"$log" 2>&1 ) || {
    echo "  ⚠ aider exited non-zero. Last 20 lines of output:"
    tail -n 20 "$log" | sed 's/^/    /'
    echo "    (full log: $log)"
  }
  dur=$(( $(date +%s) - start ))
  # Aider prints "Tokens: 1.2k sent, 300 received. Cost: $0.018 message, $0.018 session."
  cost=$(grep -oE 'Cost: \$[0-9.]+ message' "$log" | tail -1 | grep -oE '[0-9.]+' || true)
  cost=${cost:-0}
  turns=$(grep -cE '^(>|aider>)' "$log" || true)
  turns=${turns:-0}
  [ "$turns" -gt 0 ] 2>/dev/null || turns=1
  printf "   [%ds · %s turns · \$%s]\n" "$dur" "$turns" "${cost:-0}"
  total_cost_usd=$(awk "BEGIN{printf \"%.4f\", $total_cost_usd + ${cost:-0}}")
  total_turns=$((total_turns + turns))

  # Synthesize a feature-tagged batch into the gateway so the dashboard has
  # rows to filter on. The edge-proxy's TraceRecord pipeline doesn't yet bridge
  # to otel_spans (CTO-40/41 will close that loop); until then this side-channel
  # is what makes the deep links land on real data instead of a blank view.
  trace_id=$(python3 -c "import uuid;print(uuid.uuid4().hex)")
  trace_ids+=("$trace_id")
  python3 "$here/_emit_batch.py" \
    --gateway "$GATEWAY_URL/v1/batches" \
    --tenant "$TENANT" \
    --feature-tag "$FEATURE_TAG" \
    --trace-id "$trace_id" \
    --cost-usd "${cost:-0}" \
    --turns "$turns" \
    --provider "$PROVIDER" >/dev/null

  bash "$here/reset.sh"
  rm -f "$log"
done

# 6. Summary block + auto-open Workflow 1.
last_trace=""
if [[ ${#trace_ids[@]} -gt 0 ]]; then
  last_trace="${trace_ids[$((${#trace_ids[@]} - 1))]}"
fi
echo
echo "─── make aider-demo: done ────────────────────────────────────"
printf "  %d tasks · %d turns · \$%s total\n" "$i" "$total_turns" "$total_cost_usd"
echo
echo "  Workflow 1 (agents):  $(workflow1_url "$FEATURE_TAG")"
[[ -n "$last_trace" ]] && echo "    drill into tail run: $(workflow1_url "$FEATURE_TAG" "$last_trace")"
echo "  Workflow 2 (compare): $(workflow2_url "$FEATURE_TAG")"
echo "  Workflow 3 (cost):    $(workflow3_url "$FEATURE_TAG")"
echo
echo "Opening Workflow 1…"
open_url "$(workflow1_url "$FEATURE_TAG")"
