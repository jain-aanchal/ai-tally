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
