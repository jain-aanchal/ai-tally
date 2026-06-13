#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Helpers sourced by run.sh: deep-link builders + cross-platform URL opener.
# No state, no side effects on source — define functions only.

TALLY_WEB_URL="${TALLY_WEB_URL:-http://localhost:3000}"

# Build the three workflow deep links for a given feature tag + trace id.
# Workflow 1 = Agents (filtered by tag, optionally drilled into one run).
# Workflow 2 = Compare (carries the tag for CTO-105's tag-scoped replay).
# Workflow 3 = Cost (filtered by tag).
workflow1_url() {
  local tag="$1" trace="${2:-}"
  if [[ -n "$trace" ]]; then
    printf '%s/agents?tag=%s&run=%s\n' "$TALLY_WEB_URL" "$tag" "$trace"
  else
    printf '%s/agents?tag=%s\n' "$TALLY_WEB_URL" "$tag"
  fi
}

workflow2_url() {
  local tag="$1"
  printf '%s/compare?tag=%s\n' "$TALLY_WEB_URL" "$tag"
}

workflow3_url() {
  local tag="$1"
  printf '%s/cost?tag=%s\n' "$TALLY_WEB_URL" "$tag"
}

# Open a URL in the default browser. macOS uses `open`, Linux uses `xdg-open`,
# anything else just prints — never fail the demo over a missing opener.
open_url() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  else
    echo "  (open this manually: $url)"
  fi
}
