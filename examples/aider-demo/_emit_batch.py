# SPDX-License-Identifier: Apache-2.0
"""Internal helper: POST a feature-tagged span batch to the ai-tally gateway.

Used by run.sh between Aider tasks to land otel_spans rows the dashboard can
filter on. Bridging the edge-proxy's TraceRecord pipeline directly into the
otel_spans table is CTO-40/41 territory; until that lands this side-channel
keeps make aider-demo's deep links pointing at real data.

Standalone (urllib only) so it doesn't depend on the SDK being importable.
"""

from __future__ import annotations

import argparse
import json
import uuid
import urllib.request


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", required=True, help="e.g. http://localhost:8080/v1/batches")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--feature-tag", required=True)
    ap.add_argument("--trace-id", required=True, help="32-hex-char trace id")
    ap.add_argument("--cost-usd", type=float, default=0.0)
    ap.add_argument("--turns", type=int, default=1)
    ap.add_argument("--provider", default="openai")
    args = ap.parse_args()

    cost_micro = int(round(args.cost_usd * 1_000_000))
    # Split the run's cost across `turns` synthetic spans so the agent-run view
    # shows multi-step traces. Each span shares the same TraceId so they roll
    # up as one run.
    spans = []
    for step in range(max(1, args.turns)):
        per_step_cost = cost_micro // max(1, args.turns)
        attrs = {
            "ServiceName": "aider",
            "trace_id": args.trace_id,
            "span_id": uuid.uuid4().hex[:16],
            "gen_ai.system": args.provider,
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o-mini" if args.provider == "openai" else "claude-sonnet-4",
            "gen_ai.response.model": "gpt-4o-mini" if args.provider == "openai" else "claude-sonnet-4",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 250,
            "gen_ai.cost.estimated_micro_usd": per_step_cost,
            "gen_ai.cost.currency": "USD",
            "gen_ai.cost.price_catalog_version": "demo",
            "gen_ai.feature_tag": args.feature_tag,
            "gen_ai.session_id": args.trace_id,
            "gen_ai.agent.step.index": step,
        }
        spans.append(attrs)

    batch = {
        "tenant_id": args.tenant,
        "sdk_version": "aider-demo",
        "batch_id": uuid.uuid4().hex,
        "resource_spans": spans,
    }

    req = urllib.request.Request(
        args.gateway,
        data=json.dumps(batch).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        print(resp.status)


if __name__ == "__main__":
    main()
