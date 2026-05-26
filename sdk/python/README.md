# tally-sdk (Python)

The deep-context ingestion path for ai-tally. OpenTelemetry `gen_ai.*` native, with cost,
feature-tag, identity, and agent extensions.

Core invariant: **the SDK must never raise into the customer's code path.** All internal errors
are caught at the SDK boundary, recorded to self-observability, and the original call proceeds.

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
```

Zero runtime dependencies today (the schema + safety + sampling + guardrail primitives are
pure-Python). OTel/OpenLLMetry integration lands in later tickets.
