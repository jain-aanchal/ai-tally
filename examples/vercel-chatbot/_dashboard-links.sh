#!/usr/bin/env bash
# Source helper used by run.sh. Centralizes the dashboard URLs so the demo
# script can stay short.
# shellcheck disable=SC2034

TALLY_DASHBOARD="${TALLY_DASHBOARD:-http://localhost:3000}"
TAG="${TALLY_FEATURE_TAG:-chatbot-demo}"

WORKFLOW_2="${TALLY_DASHBOARD}/compare?tag=${TAG}"
WORKFLOW_3="${TALLY_DASHBOARD}/cost?tag=${TAG}"
WORKFLOW_4="${TALLY_DASHBOARD}/attribution?tag=${TAG}&outcome=positive_feedback"

print_links() {
  echo "  Workflow 2 (Cross-provider): ${WORKFLOW_2}"
  echo "  Workflow 3 (Feature cost):   ${WORKFLOW_3}"
  echo "  Workflow 4 (Attribution):    ${WORKFLOW_4}"
}

open_workflow_4() {
  echo "  Opening Workflow 4…"
  if command -v open >/dev/null 2>&1; then
    open "${WORKFLOW_4}" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${WORKFLOW_4}" >/dev/null 2>&1 || true
  fi
}
