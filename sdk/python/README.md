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

# From here, unmodified provider calls are metered automatically - no record_* calls:
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
  key. Until the bootstrap completes (or if it fails), accounts land unattributed - never a raw id.
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

## Onboarding MCP server (CTO-261)

`tally.init()` meters the LLM layer on its own, but the other layers need app code: the
vector, tool and embedding `record_*` calls, and the per-customer attribution only your app
can resolve. The onboarding MCP server hands your own coding agent the maintained recipes and
generated snippets for that wiring. Your source never leaves your machine: the server holds
only the recipe catalog and the SDK surface, and it returns code, never an applied edit.

Install the extra (it is optional, the SDK runtime itself has no dependencies):

```bash
pip install 'tally-sdk[mcp]'
```

That installs the `tally-onboarding-mcp` console script, which speaks MCP over stdio.

### Claude Code

```bash
claude mcp add ai-tally-onboarding -- tally-onboarding-mcp
```

Or add it to `.mcp.json` in your project root so your team picks it up too:

```json
{
  "mcpServers": {
    "ai-tally-onboarding": {
      "command": "tally-onboarding-mcp",
      "args": []
    }
  }
}
```

### Cursor

Add the same stanza to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "ai-tally-onboarding": {
      "command": "tally-onboarding-mcp",
      "args": []
    }
  }
}
```

If `tally-onboarding-mcp` is not on your agent's PATH, point `command` at the interpreter that
has it installed instead: `"command": "/path/to/.venv/bin/tally-onboarding-mcp"`.

### The tools

| Tool | What it returns |
|---|---|
| `detect_stack` | providers, agent frameworks, vector DBs and web frameworks found in a manifest, plus the recipe ids that match and the gaps that matched nothing |
| `get_recipe` | one machine-readable recipe, by id or by a friendly name (`pinecone`, `fastapi`) |
| `generate_startup` | the `tally.init()` line for application startup |
| `generate_middleware` | account / feature middleware bound to the resolver you confirm, bundled with the startup line |
| `instrument_call_site` | the adapted `record_*` edit for one concrete call site |
| `explain_layer` | which `record_*` method covers a layer and why, grounded on the live SDK surface |
| `coverage_report` | per-layer coverage read from the gateway's probe: which layers a real span proves are flowing, and why each dark layer is dark |

#### Configuring `coverage_report`

`coverage_report` is the only tool that talks to anything outside your machine. It calls the
gateway's coverage probe, which is the side that can read your telemetry, so it needs three
things from the environment:

| Variable | What it is |
|---|---|
| `TALLY_GATEWAY_URL` | base URL of the gateway that holds your telemetry. Must be `https://`, because the request carries the service token; plain `http://` is accepted only for `localhost`, `127.0.0.1` or `::1` so local dev still works |
| `TALLY_TENANT_ID` | your tenant UUID, sent as `x-tenant-id` |
| `GATEWAY_SERVICE_TOKEN` | the control-plane service token. Set `TALLY_GATEWAY_SERVICE_TOKEN_ENV` to read it from a differently named variable instead |

The token is held by reference: the tool reads the variable at the moment it calls the probe and
never stores, echoes or logs the value. The `tenant_key` argument is likewise reported only as
present or absent, never sent onward.

The token is also never carried off the origin you configured: a redirect to a different scheme,
host or port is refused rather than followed, so a load balancer that 301s elsewhere cannot be
handed your service token.

Leave these unset and the tool answers `probe_available: false` with every layer `unknown` and
the reason attached. That is also what you get if `TALLY_GATEWAY_URL` is rejected as unsafe, or
if the gateway is unreachable, answers an error, times out, redirects off-origin, or returns
something unreadable or oversized. The result shape is the same on both paths, so `reason` is
always present (empty when the probe answered). None of those ever turns into "this layer is not
wired": a layer is reported covered only when the probe returns a span count above zero to prove
it, re-derived from that count rather than taken on the wire's word, so a payload claiming
coverage with no evidence is downgraded back to `unknown`.

Pass `wired` (the layers you just instrumented) to separate "wired, awaiting first event" from
"not wired" on a dark layer. It only softens the wording; it can never produce coverage.

Two things the server will not do. It never guesses which customer a request belongs to: pass
`generate_middleware` the header or resolver you confirmed, and with no answer it returns a
reported gap and the account layer stays unattributed. And it never invents an SDK call: a
stack with no recipe comes back as a gap, and every hole it cannot fill from the call site you
passed is left as a visible `<FILL:...>` marker for you to complete.

Both mcp 1.x (`FastMCP`) and mcp 2.x (`MCPServer`) are supported. Without the extra installed,
launching the server fails with a clear error rather than starting a server that does nothing.

## Hosted repo PR bot (CTO-261)

The MCP server above hands the recipes to your own coding agent. The PR bot is the other end
of the same catalog: give it scoped access to a repo and it runs the loop server-side and
opens a reviewed pull request. Same recipes, same refusals, no coding agent needed on your
side. It is a headless bot rather than a GitHub App, so the access you grant is a token you
hold and can take back.

```bash
export TALLY_ONBOARDING_GITHUB_TOKEN=github_pat_...
tally-onboarding-bot \
  --repo acme/widgets \
  --account-source 'request.headers.get("X-Customer-Id")' \
  --feature-tag support-bot
```

Omit `--account-source` and the bot does not pick one for you: it opens the PR carrying the
account question and the candidate resolvers it found, leaves the account layer
unattributed, and says so in the body. `--dry-run` proposes the diff and stops before
creating a branch.

### Supplying and revoking the token

1. In GitHub, go to Settings, Developer settings, Personal access tokens, Fine-grained
   tokens, and generate a token whose repository access is **only** the repo you want the PR
   in.
2. Give it exactly two repository permissions: **Contents: read and write** (to push the new
   branch) and **Pull requests: read and write** (to open the PR). Nothing else is used. Set
   the shortest expiry you can live with.
3. Put the value in the environment variable named by `--token-env` (default
   `TALLY_ONBOARDING_GITHUB_TOKEN`), or in your secret manager and inject it from there. The
   bot holds the variable NAME, never the value: it reads it at the moment git or the API
   needs it, hands it to git through `GIT_ASKPASS` so it never lands in `.git/config` or in a
   process listing, and redacts it from anything it prints.
4. To revoke, delete the token on that same page, or unset the variable. There is no
   installation to uninstall and no stored copy to clean up, which is the point of a token
   you hold rather than an app you grant.

A GitHub App is the harder-edged version of this (short-lived installation tokens, grants
managed in GitHub) and stays the hardening path. It would change how the credential is
minted, not what the bot is allowed to do.

### What it refuses to do

Enforced in `onboarding_bot/guards.py` and covered by `tests/test_onboarding_bot_guards.py`,
not merely promised here:

- **It never pushes to a default branch.** Every push resolves the repo's default branch
  first and refuses it, along with `main` / `master` / `trunk` / `develop` whatever the
  remote reports. It pushes the one new branch it created, and force pushes are refused.
- **It never merges.** Git subcommands and GitHub endpoints are both allowlisted; `merge`
  (and `rebase`, `cherry-pick`, `reset`) is on neither, so no code path reaches one. The PR
  waits for a human.
- **It keeps no source.** The clone is shallow, lives in a temporary directory, and is
  deleted in a `finally` on every exit path including a refused run. What the run returns is
  paths, counts, generated code and gaps, never your source.
- **It invents nothing.** Every line it writes comes from the recipe catalog. A value it
  cannot derive from the call site stays a visible `<FILL:...>` hole, and a block with a hole
  is inserted commented out so nothing runs on a guessed value.
