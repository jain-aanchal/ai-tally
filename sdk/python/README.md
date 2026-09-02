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

## One-line connect (CTO-260)

The fastest path. `tally.init(key)` needs no `tenant_id` (the ingest key is tenant-bound at the
gateway), auto-instruments the official `openai` and `anthropic` clients, boots the per-tenant
HMAC key off-thread, and installs a background batching transport to `/v1/batches`.

```python
import tally

tally.init("tally_sk_live_...")   # falls back to TALLY_KEY / TALLY_ENDPOINT env

# From here, unmodified provider calls are metered automatically — no record_* calls:
client.chat.completions.create(model="gpt-4o-mini", messages=[...])   # openai
client.messages.create(model="claude-sonnet-4-5", messages=[...])     # anthropic
```

`init` is idempotent, never blocks the calling thread, and never raises: a bad key, an unreachable
gateway, or a missing provider library degrades to unattributed or disabled instrumentation with a
one-time warning. Sync, async (`AsyncOpenAI` / `AsyncAnthropic`), and streaming are all covered.

- **Streaming tokens.** OpenAI reports usage only when `stream_options={"include_usage": True}` is
  set. By default a stream without it emits a span with **null** token counts (honest blank, never
  a fabricated zero). Pass `tally.init(..., instrument_stream_usage=True)` to have the OpenAI
  wrapper add `include_usage` when the caller did not, so streamed calls price fully.
- **Accounts.** Set the customer once with `with_account("acct_...")`; every auto-instrumented span
  in the scope carries the HMAC'd account hash, computed in-process under the bootstrapped tenant
  key. Until the bootstrap completes (or if it fails), accounts land unattributed — never a raw id.
- **What is automatic vs. app-side.** The one-liner captures LLM provider calls only. Vector search
  (`tally.record_vector_call`), your own tool calls (`tally.record_tool_call`), and embeddings not
  made through the patched client (`tally.record_embedding_call`) remain explicit one-liners that
  delegate to the process-global client. These are safe no-ops before `init`.
- **Lifecycle.** `tally.flush()` drains buffered spans (also drained at `atexit`); `tally.uninstrument()`
  reverses all patches and tears the client down (used by tests).

### Hashing an account for the proxy path

The zero-code proxy holds no HMAC key. To send a pre-hashed `X-Tally-Account-Id-Hash`, compute it
on your own machine with the same key the SDK uses:

```python
from tally import hash_account
h = hash_account("acct_northwind")          # uses the bootstrapped tenant key
```

```bash
python -m tally.hash_account acct_northwind  # CLI form, reads TALLY_KEY / TALLY_ENDPOINT
```

> The gateway endpoint `GET /v1/tenant/hmac-key` that the bootstrap fetches is delivered in a
> separate PR; the SDK codes against its contract (spec §3.2).

## Tagging spend with a customer account

An `account_id` says which of *your* customers a call belongs to. It is what turns a cost total
into cost per customer.

The id is **context-scoped**: a web app knows the customer once, at request start, so you set it
once and every span inside the scope picks it up. Requiring it on every call would be noise, and
noise gets skipped.

```python
from tally.client import TallyClient
from tally.context import start_trace, with_account
from tally.hmac_keys import HmacKeyRegistry

registry = HmacKeyRegistry()
registry.provision("tenant-a")
client = TallyClient(tenant_id="tenant-a", hmac_registry=registry)

# In a request middleware: resolve the customer once, wrap the handler.
with start_trace(feature_tag="support-bot"), with_account("acct_northwind"):
    client.record_llm_call(provider="openai", model="gpt-4o", usage=usage)
    client.record_tool_call(provider="openai", tool="web_search")
    # ...every span emitted inside this block carries the same account.
```

One call can override the scope, which is what a batch job that walks several customers needs:

```python
with start_trace():
    for account in accounts:
        client.record_llm_call(
            provider="openai", model="gpt-4o", usage=usage, account_id=account
        )
```

`with_account(None)` clears the account for a block. That is the opt-out for a background task
that must not inherit its caller's customer.

### What actually goes on the wire

The raw `account_id` never leaves your process. It is HMAC-SHA256'd under your **per-tenant** key
at emit time, exactly like a user id, and the span carries only:

| Attribute | Meaning |
|---|---|
| `gen_ai.account_id_hash` | HMAC-SHA256 hex of the account id |
| `gen_ai.account_id_hash_key_version` | the key version that produced it, so rotation does not orphan history |
| `gen_ai.account_label` | optional display name (see below) |

Because the key is per tenant, the same account id hashes differently for two different tenants,
so an account cannot be correlated across them.

If the client has no `tenant_id` or no `hmac_registry`, the span is emitted **unattributed** with a
one-time warning. It is never dropped and the raw id is never substituted.

### The optional label

```python
with with_account("acct_northwind", label="Northwind Traders"):
    ...
```

The label is **wire-only**. The gateway upserts it into a label store keyed on the account hash
and does not write it to the span row, so no customer name lands in the telemetry store. Labels
are optional per account: set none and the dashboard falls back to a shortened hash, which is a
supported way to run. A label with no account id alongside it is dropped, since there is nothing
to key it on.
