"""Smoke client: record a couple of LLM calls and POST them to the local gateway.

Usage (after `make up`):
    python infra/gateway/examples/send_batch.py
    python infra/gateway/examples/send_batch.py --api-key tally_sk_...   # when auth is enabled

Then check ClickHouse:
    curl 'http://localhost:8123/?user=tally&password=tally' \
      --data-binary "SELECT FeatureTag, count(), sum(EstimatedCost) FROM otel_spans GROUP BY FeatureTag"
"""

from __future__ import annotations

import argparse
import json
import urllib.request

from tally.pricing import Usage, compute_cost_micro_usd, seed_catalog
from tally.schema import SpanFields, build_span_attributes
from tally.wire import uuid7

CATALOG = seed_catalog()


def make_span(feature: str, model: str, in_tok: int, out_tok: int) -> dict[str, object]:
    cost, version = compute_cost_micro_usd(CATALOG, "openai", model, Usage(in_tok, out_tok))
    attrs = build_span_attributes(
        SpanFields(
            system="openai",
            request_model=model,
            response_model=model,
            operation="chat",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_estimated_micro_usd=cost,
            price_catalog_version=version,
            feature_tag=feature,
            session_id=uuid7(),
        )
    )
    attrs["ServiceName"] = "demo-app"
    # Real SDK spans carry trace/span ids from context; the gateway dedupes on (trace_id, span_id),
    # so give each demo span its own pair (otherwise they collapse to one).
    attrs["trace_id"] = uuid7().replace("-", "")
    attrs["span_id"] = uuid7().replace("-", "")[:16]
    return attrs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080/v1/batches")
    ap.add_argument("--tenant", default="local-dev")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    batch = {
        "tenant_id": args.tenant,
        "sdk_version": "demo",
        "batch_id": uuid7(),
        "resource_spans": [
            make_span("assistant", "gpt-5-mini", 1200, 300),
            make_span("assistant", "gpt-5", 4000, 900),
            make_span("summarize", "gpt-5-mini", 8000, 120),
        ],
    }

    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(args.url, data=body, headers={"Content-Type": "application/json"})
    if args.api_key:
        req.add_header("Authorization", f"Bearer {args.api_key}")
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode("utf-8"))

    # demonstrate idempotency: replaying the same batch_id returns replayed=true, no new rows
    with urllib.request.urlopen(req) as resp:
        print("replay:", resp.read().decode("utf-8"))


if __name__ == "__main__":
    main()
