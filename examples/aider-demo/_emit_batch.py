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
    # The gateway's enrich_cost recomputes the authoritative cost from the price
    # catalog. The seed catalog only knows gpt-5-mini / gpt-5 — anything else is
    # a catalog miss and the cost gets dropped to 0. Until the catalog grows
    # real provider models, we pin to gpt-5-mini and back-compute output tokens
    # from Aider's reported cost so the dashboard number matches what Aider saw.
    #
    # gpt-5-mini output rate is $2.00 / 1M tokens → 2 micro-USD/token.
    MICRO_PER_OUTPUT_TOKEN = 2  # gpt-5-mini output rate
    DASHBOARD_MODEL = "gpt-5-mini"
    DASHBOARD_SYSTEM = "openai"

    # Split the run's cost across `turns` synthetic spans so the agent-run view
    # shows multi-step traces. Each span shares the same TraceId so they roll
    # up as one run.
    spans = []
    per_step_cost = cost_micro // max(1, args.turns)
    output_tokens_per_step = max(1, per_step_cost // MICRO_PER_OUTPUT_TOKEN)
    for step in range(max(1, args.turns)):
        attrs = {
            "ServiceName": "aider",
            "trace_id": args.trace_id,
            "span_id": uuid.uuid4().hex[:16],
            "gen_ai.system": DASHBOARD_SYSTEM,
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": DASHBOARD_MODEL,
            "gen_ai.response.model": DASHBOARD_MODEL,
            "gen_ai.usage.input_tokens": 0,
            "gen_ai.usage.output_tokens": output_tokens_per_step,
            "gen_ai.cost.estimated_micro_usd": per_step_cost,
            "gen_ai.cost.currency": "USD",
            # NOTE: real provider was args.provider; pinned to openai/gpt-5-mini for
            # the dashboard so the price-catalog enrichment doesn't drop the cost.
            # Real provider preserved in the long-tail attributes below.
            "aider.real_provider": args.provider,
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
