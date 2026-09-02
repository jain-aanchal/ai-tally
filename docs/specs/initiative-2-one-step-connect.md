# Initiative 2: Real one-step connect

Status: draft spec, build-ready. Owner: platform. Ticket: CTO-261 (umbrella).

This is a design spec. It defines the decisions, endpoints, wrapped-method list,
config changes, and file-level change list. It does not contain the
implementation.

Depends on Initiative 1 (Organizations, users & access, CTO-260,
`docs/specs/initiative-1-orgs-and-access.md`). This spec reuses Initiative 1's
decisions verbatim and does not re-litigate them: Clerk owns dashboard identity;
ai-tally owns per-org ingest API keys in its own control plane (the `api_keys`
table, SHA-256 hashed, minted as `tally_sk_live_...`, shown once); canonical
ClickHouse `TenantId` is the tenant UUID.

## 1. Summary, goals, non-goals

### Summary

Today, instrumenting an app against ai-tally is a hand-assembly job. A developer
constructs a `tally.hmac_keys.HmacKeyRegistry`, calls `registry.provision(tenant)`,
constructs a `tally.client.TallyClient(tenant_id=..., hmac_registry=...)`, wraps
their handler in `tally.context.start_trace(feature_tag=...)` /
`with_account(...)`, and then calls `client.record_llm_call(...)` /
`record_tool_call(...)` by hand at every provider call
(`sdk/python/README.md`). The zero-code proxy exists
(`infra/edge-proxy/`) but each proxy instance pins exactly one provider origin
via `EDGE_PROXY_UPSTREAM` (`infra/edge-proxy/internal/config/config.go`), so it
is not yet a single hosted endpoint an org can point every provider at.

This initiative turns "instrument your app" into one line, using a **real
per-org key** minted in Initiative 1. There is nothing pre-provisioned and
nothing hardcoded. Two paths share the same key and resolve to the same tenant:

- **Zero-code (proxy).** Point `OPENAI_BASE_URL` (or the Anthropic base URL) at
  the hosted ai-tally proxy and send the org's key as `X-Tenant-Key`. Any
  language, no library.
- **Deep context (SDK).** `pip install tally` then `tally.init(key)`
  auto-instruments the official `openai` and `anthropic` clients, with no manual
  `record_*` calls. This path adds per-tool, per-account, and per-agent depth the
  proxy cannot see.

Success: a signed-up org goes from creating a key to a populated Cost Explorer in
under 5 minutes, priced from the built-in catalog, with no config file.

### Goals

1. One line to connect, per path, using a real per-org key. No shared key, no
   pre-provisioned key, no hardcoded tenant.
2. `tally.init(key)` that needs no `tenant_id` (the key is tenant-bound at the
   gateway) and auto-instruments the official `openai` / `anthropic` clients:
   sync, async, streaming, and errors.
3. An authenticated in-process HMAC bootstrap so the SDK can hash account and
   user ids client-side without the caller passing a tenant id, and without raw
   ids ever leaving the customer process.
4. A background, non-blocking ingest transport that batches spans to
   `/v1/batches` with the key as bearer, with buffering, retry, and backpressure.
5. One hosted proxy endpoint that serves OpenAI, Anthropic, and further providers,
   with fast key-to-tenant resolution at the edge that holds the p99 < 3ms budget.
6. Zero-setup pricing: the built-in catalog prices both paths.
7. First-data onboarding: a live "we received your first event" state and a
   copy-paste snippet generator per path and language, shown right after key
   creation.

### Non-goals

- **Building Initiative 1.** Orgs, Clerk auth, the `api_keys` table, key mint /
  rotate / revoke, and canonical `TenantId = UUID` are delivered there. This
  spec consumes them (§10).
- **Dashboard authentication.** Clerk sessions and org gating are Initiative 1.
- **The Goose demo.** Out of scope; it builds on this initiative separately.
- **Broker mode / BYO-VPC changes.** The self-host key-broker path
  (`EDGE_PROXY_MODE=broker`, CTO-43) is unchanged here. This initiative is about
  the hosted cloud path.
- **New providers beyond OpenAI and Anthropic in P1.** Gemini extraction already
  exists in the proxy (`config.ProviderGemini`); wiring a third provider into the
  hosted router and SDK is a fast-follow, not a P1 gate.
- **Prompt / completion capture.** No bodies in telemetry, ever (§11).

## 2. Decisions

1. **One key, both paths, same tenant.** The org's ingest key
   (`tally_sk_live_...`, minted and hashed in Initiative 1) is the only
   credential. The SDK sends it as an HTTP bearer to `/v1/batches`; the proxy
   accepts it as `X-Tenant-Key`. Both resolve, via the SHA-256 hash lookup in
   `api_keys`, to the same tenant UUID. Nothing is pre-provisioned; the key is
   created by the org in the dashboard.

2. **The SDK auto-instruments by monkeypatching the official clients.** `tally.init`
   patches `openai` and `anthropic` client methods in place, so an unmodified
   `client.chat.completions.create(...)` or `client.messages.create(...)` emits a
   conformant span. There are no manual `record_*` calls on the golden path. The
   existing pluggable instrumentor (`sdk/python/src/tally/instrumentation/`) is
   the seam this builds on (§4).

3. **The hosted proxy serves multiple providers from one endpoint.** Today
   `EDGE_PROXY_UPSTREAM` pins one origin per instance. The hosted deployment adds
   per-request routing (§6) so one URL serves OpenAI and Anthropic. Bodies stay
   byte-for-byte untouched and the p99 budget is preserved.

4. **Pricing is zero-setup from the built-in catalog.** The gateway prices every
   ingested span authoritatively via `enrich_cost(span, catalog, tenant_id=...)`
   over `seed_catalog()` (`infra/gateway/src/gateway/app.py`), for both paths.
   The org configures nothing (§8).

5. **Nothing pre-provisioned or hardcoded.** No default tenant, no bundled key,
   no `local-dev` on the product path (Initiative 1, Decision 4). The per-tenant
   HMAC key is minted per org at provision (Initiative 1, §4) and fetched by the
   SDK at runtime under the org's own key (§3).

## 3. SDK `tally.init(key, ...)`

### The simplification

Because the key is tenant-bound at the gateway, the SDK does not need a
`tenant_id`. `tally.init` takes the key and derives everything else.

```python
import tally

tally.init(key="tally_sk_live_...")          # reads TALLY_KEY env if key omitted
# ... from here, openai / anthropic calls are auto-instrumented (§4)
```

Signature (new module `sdk/python/src/tally/init.py`, exported from
`sdk/python/src/tally/__init__.py`):

```python
def init(
    key: str | None = None,          # falls back to env TALLY_KEY
    *,
    endpoint: str | None = None,     # ingest base URL; default the hosted gateway, env TALLY_ENDPOINT
    feature_tag: str | None = None,  # optional default feature tag for the process
    instrument: bool = True,         # monkeypatch openai/anthropic if installed
    flush_interval_s: float = 1.0,   # background batcher cadence (§5)
    catalog: PriceCatalog | None = None,  # default: bundled seed_catalog (§8)
) -> TallyClient: ...
```

`init` is idempotent (a second call returns the same process-global client and
does not double-patch). It **never raises into the caller**: a bad key, an
unreachable gateway, or a missing provider library degrades to unattributed or
disabled instrumentation with a one-time warning, per the SDK's core invariant
(`sdk/python/README.md`). `init` does no blocking network I/O on the calling
thread; the HMAC bootstrap and transport run off-thread (§3.2, §5).

### 3.1 Tenant resolution

The SDK does not learn or store the tenant UUID as a required field. The key is
authoritative: the gateway maps `sha256(key)` to a tenant on every ingest request
(`ApiKeyAuth.authenticate`, `infra/gateway/src/gateway/auth.py`) and refuses a
body claiming a different tenant with `TENANT_MISMATCH`
(`infra/gateway/src/gateway/app.py`). So the SDK omits `tenant_id` from the
`/v1/batches` envelope entirely and lets the key decide. The tenant UUID is
returned once by the bootstrap (§3.2) and used only as a local hash-registry key,
never sent on the wire.

### 3.2 HMAC key bootstrap (new endpoint)

To hash account and user ids client-side (so raw ids never leave the process),
the SDK needs the tenant's current per-tenant HMAC key material and its version.
`init` fetches it once, authenticated by the same ingest key, and caches it in
process memory.

New gateway endpoint:

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/v1/tenant/hmac-key` | Return the caller tenant's active HMAC key material + version for in-process hashing. | ingest key (bearer) |

Behavior:

- Auth reuses `ApiKeyAuth.authenticate` (SHA-256 of the bearer). The tenant is
  the key's tenant; there is no `x-tenant-id` input, so one org can never fetch
  another's key material. A `read`, `write`, or `admin` scope may fetch it (it is
  the tenant's own key, used only to hash the tenant's own data).
- Response `200`:
  ```json
  {
    "tenant_id": "<uuid>",
    "key_version": "v3",
    "key_material_b64": "<base64 of the active HMAC key bytes>",
    "algorithm": "HMAC-SHA256"
  }
  ```
- The gateway resolves the material through the existing
  `tally.hmac_keys.KeyMaterialProvider` seam (KMS/Vault in prod), the same
  material the gateway itself uses to hash Stripe customer emails
  (`app.state.hmac_registry`, `infra/gateway/src/gateway/app.py`). It returns the
  **active version only**. Historical versions are not exported; the SDK stamps
  new spans with the active version, and rotation history (the identity-graph
  edges) stays server-side (`sdk/python/src/tally/hmac_keys.py`, Option B
  rotation).

Why this is safe (and what it is not): the exported bytes are the tenant's own
key, delivered only to a process already holding that tenant's ingest key, used
only to hash that tenant's own account/user ids inside that process. This is the
same trust boundary as the existing SDK, which already derives per-tenant
material locally via `InMemoryKeyMaterialProvider`. The endpoint is not a general
KMS proxy: it returns one tenant's one active symmetric key, never a KEK, never
another tenant's key, never raw provider credentials.

Handling and caching rules:

- **Sensitive in memory.** The material is held only in the process
  `HmacKeyRegistry`, never written to disk, never logged, never placed on a span
  or in the ingest envelope. Treated as a secret.
- **Cached with a TTL and re-fetched on rotation.** Cache the material for a
  bounded TTL (for example 1h). If the gateway later rejects a span's stamped
  version as stale, or on TTL expiry, re-fetch. A key rotation server-side means
  the next fetch returns a higher `key_version`; old spans keep their stamped
  version so history is not orphaned.
- **Transport is TLS only.** The endpoint is refused over plaintext by the hosted
  gateway.

### 3.3 Fallback: unattributed, never a raw id

If the bootstrap fails (network down, endpoint disabled, key lacks reach), the
SDK does not block `init` and does not substitute a raw id. It behaves exactly
like today's no-HMAC client: account and user ids are emitted **unattributed**
(the `UNATTRIBUTED` sentinel, `sdk/python/src/tally/account_identity.py`) with a
one-time warning, and the raw id is never placed on the wire
(`sdk/python/README.md`: "If the client has no `tenant_id` or no `hmac_registry`,
the span is emitted unattributed with a one-time warning. It is never dropped and
the raw id is never substituted"). LLM cost and model spans still flow; only the
per-customer dimension is missing until the key can be fetched. This preserves
"honest under uncertainty": a blank account, never a fabricated or raw one.

## 4. Auto-instrumentation

`init(instrument=True)` monkeypatches the official clients if they are importable.
It never imports them itself as a hard dependency (the SDK keeps zero required
runtime deps, `sdk/python/README.md`); it patches only what is already installed
in the customer's environment. This builds on the existing pluggable
`ProviderInstrumentor` seam (`sdk/python/src/tally/instrumentation/base.py`,
`wrap_create`), which already runs span-building inside the never-crash
`safe_block` boundary while letting the provider's own exceptions propagate
unchanged.

### 4.1 What is wrapped

New module `sdk/python/src/tally/instrumentation/patch.py` with `patch_openai()`
and `patch_anthropic()`, plus a new `AnthropicInstrumentor`
(`sdk/python/src/tally/instrumentation/anthropic.py`) mirroring the existing
`OpenAIInstrumentor` (`sdk/python/src/tally/instrumentation/openai.py`).

OpenAI (`openai>=1.x`), patched on the client classes so every instance is
covered:

| Wrapped method | Sync/async | Notes |
| --- | --- | --- |
| `OpenAI().chat.completions.create` | sync | existing `OpenAIInstrumentor` extraction |
| `AsyncOpenAI().chat.completions.create` | async | async wrapper variant (§4.2) |
| `OpenAI().responses.create` | sync | Responses API; usage under `response.usage` |
| `AsyncOpenAI().responses.create` | async | |
| `OpenAI().embeddings.create` | sync | maps to `record_embedding_call` cost path |
| `AsyncOpenAI().embeddings.create` | async | |

Anthropic (`anthropic>=0.x`):

| Wrapped method | Sync/async | Notes |
| --- | --- | --- |
| `Anthropic().messages.create` | sync | usage `input_tokens` / `output_tokens` |
| `AsyncAnthropic().messages.create` | async | |
| `Anthropic().messages.stream` | sync (context manager) | accumulate usage (§4.3) |
| `AsyncAnthropic().messages.stream` | async (context manager) | |

The patch is applied at the unbound method / class-attribute level so clients
constructed after `init` are also instrumented, and is reversible via a stored
handle (`tally.uninstrument()`), which tests use to avoid cross-test leakage.
Double-patching is a no-op (the wrapper is tagged with a sentinel attribute and
skipped if already present).

### 4.2 Usage extraction

Each provider instrumentor is a pure function over the response object
(`ProviderInstrumentor` protocol). Extraction, verified against the current
code and the proxy's Go extractors (`infra/edge-proxy/internal/proxy/provider.go`):

- **OpenAI chat**: `usage.prompt_tokens`, `usage.completion_tokens`,
  `usage.prompt_tokens_details.cached_tokens` (already implemented,
  `OpenAIInstrumentor.extract_usage`). Model from `response.model`.
- **OpenAI responses**: `usage.input_tokens`, `usage.output_tokens`.
- **OpenAI embeddings**: `usage.prompt_tokens` as input; no output tokens; priced
  via `PriceType.EMBEDDING`.
- **Anthropic messages**: `usage.input_tokens`, `usage.output_tokens`,
  `usage.cache_read_input_tokens` as cached input where present. Model from
  `response.model`.

The instrumentor produces a `tally.pricing.Usage` and calls into `build_span`,
which pulls `feature_tag` / `session_id` from the active trace context and, when
a catalog is present, computes cost via `compute_cost_micro_usd` (integer
micro-USD, `Decimal` rate math). The account dimension comes from the trace
context (§4.4), hashed under the bootstrapped HMAC key.

### 4.3 Streaming

Streaming responses report usage only at the end of the stream (OpenAI requires
`stream_options={"include_usage": True}`; Anthropic emits a final
`message_delta` usage). The wrapper must not buffer or alter the stream and must
not force options onto the caller's request. Approach:

- Wrap the returned iterator/async-iterator (or the `stream()` context manager)
  in a thin pass-through that yields each chunk untouched and accumulates
  usage/model from the terminal usage event. The span is emitted once, when the
  stream is exhausted or the context manager exits.
- If the caller did not enable usage on an OpenAI stream, there is no usage to
  accumulate: emit the span with token counts null (not zero) and a reason, per
  "honest under uncertainty". Do not fabricate counts. The proxy path has the
  same limitation, tracked as CTO-40 (§6, §8).
- Accumulation state is per-stream and never shared, so concurrent streams do not
  cross-contaminate.

### 4.4 Account / feature / agent depth

The SDK path keeps the context primitives (`tally.context.start_trace`,
`with_account`, per-agent feature tags). The one-line onboarding does not
require them, but they are what make the SDK path deeper than the proxy: a user
sets `with_account("acct_...")` once at request start and every auto-instrumented
span inside the scope carries the HMAC'd account hash. Tool calls
(`record_tool_call`) and per-agent tags remain explicit opt-in on top of the
auto-instrumented LLM spans.

### 4.5 Never raise

The provider call itself is never wrapped in a try/except that could swallow the
customer's real API error (`wrap_create` calls `create_fn(*args, **kwargs)`
outside the safety boundary, by design). Only span building and hand-off to the
transport run inside `safe_block`, which records to self-observability and lets
the original result through. A bug in extraction, pricing, or transport can never
change the value the caller receives or raise into their code. This is the SDK's
non-negotiable invariant.

## 5. SDK ingest transport

`init` installs a background batching exporter as the client's `Exporter`
(`TallyClient` accepts a pluggable `Exporter`,
`sdk/python/src/tally/client.py`). New module
`sdk/python/src/tally/transport.py`.

- **Envelope.** Reuse `tally.wire.BatchRequest` / `BatchResponse` and `uuid7()`
  batch ids (`sdk/python/src/tally/wire.py`). The batch omits an explicit tenant;
  the bearer key decides (§3.1). Idempotency: `(tenant_id, batch_id)` dedup for
  24h is enforced gateway-side and honored by resend-on-retry with the same
  `batch_id`.
- **Auth.** `Authorization: Bearer <key>` on every `POST {endpoint}/v1/batches`.
- **Background flush, never blocks.** Spans enqueue onto an in-memory bounded
  queue; a daemon worker thread flushes on `flush_interval_s` or when a size
  threshold is reached. The calling thread never does network I/O and never
  blocks on the queue (a full queue drops oldest with a counter, see
  backpressure). A `tally.flush()` and `atexit` hook drain on shutdown with a
  bounded timeout so a short-lived script still ships its spans.
- **Buffering + retry.** Failed flushes retry with capped exponential backoff and
  jitter. Batches are retried whole (idempotent by `batch_id`). Retries are
  bounded; exhausted batches are dropped with a counter, never retried forever.
- **Backpressure.** The queue is bounded. On sustained gateway unavailability the
  exporter sheds load (drop-oldest) and surfaces a self-observability counter,
  rather than growing memory without bound or blocking the app. This mirrors the
  gateway's own backpressure posture (`infra/gateway/src/gateway/backpressure.py`).
- **Receiving side.** The gateway already fronts ingest with an
  `AsyncIngestBuffer` (`infra/gateway/src/gateway/ingest_buffer.py`,
  `app.state.ingest_buffer`), so the SDK's batches land on a buffered, async
  write path. No new gateway ingest surface is needed for the SDK path; it posts
  to the existing `/v1/batches`.

Async apps: the transport is thread-based and framework-agnostic, so it works
under sync and asyncio callers alike without the SDK owning an event loop. The
async provider wrappers (§4.2) only enqueue; they never await the transport.

## 6. Hosted proxy, multi-provider

Today each proxy instance pins one origin (`EDGE_PROXY_UPSTREAM`, default
`https://api.openai.com`, `infra/edge-proxy/internal/config/config.go`) and
already understands per-provider response shapes via `config.Provider`
(`ProviderOpenAI`, `ProviderAnthropic`, `ProviderGemini`) and `extractMeta`
(`infra/edge-proxy/internal/proxy/provider.go`). The gap is serving multiple
providers behind one hosted URL and resolving real keys fast.

### 6.1 One endpoint, per-request routing

Add a routing layer so a single hosted deployment forwards to the right provider
origin per request, keeping bodies untouched and the p99 budget. Two mechanisms,
both supported, host-based preferred for the hosted product:

- **Host-based (preferred).** Distinct hostnames map to providers:
  `openai.proxy.ai-tally.com` to `https://api.openai.com`,
  `anthropic.proxy.ai-tally.com` to `https://api.anthropic.com`. The customer
  sets `OPENAI_BASE_URL=https://openai.proxy.ai-tally.com/v1` (or the Anthropic
  base URL). Routing is a hostname-to-origin lookup, no body inspection, so the
  hot path stays byte-identical.
- **Path-based (fallback).** A single host with a provider prefix
  (`/openai/...`, `/anthropic/...`) stripped before forwarding. Useful where a
  customer cannot set distinct hostnames.

New config: replace the single `EDGE_PROXY_UPSTREAM` with a route table for the
hosted deployment while keeping single-origin mode for self-host back-compat.
Proposed env:

| Var | Meaning |
| --- | --- |
| `EDGE_PROXY_ROUTES` | JSON or comma list mapping host (or path prefix) to `{upstream, provider}`, e.g. `openai.proxy.ai-tally.com=https://api.openai.com:openai`. |
| `EDGE_PROXY_ROUTE_MODE` | `host` (default hosted) or `path`. |

When `EDGE_PROXY_ROUTES` is unset, the proxy falls back to the existing single
`EDGE_PROXY_UPSTREAM` + `EDGE_PROXY_PROVIDER` behavior, so self-host and the
current tests are unaffected. The route's `provider` selects the existing
`extractMeta` branch, so model/usage extraction is per route with no new parser.
Invariants from `infra/edge-proxy/README.md` (bodies never mutated, telemetry
metadata-only, provider key in-flight only, stateless, streaming-safe,
`FlushInterval = -1`) are unchanged: routing is a pre-forward origin selection,
nothing more.

### 6.2 Fast key-to-tenant resolution at the edge

The proxy must map the real `X-Tenant-Key` (`tally_sk_live_...`) to a tenant UUID
without a per-request gateway round-trip, to hold p99 < 3ms.

- **Local cached view of `api_keys`.** The proxy holds an in-memory map of
  `sha256(key) -> {tenant_uuid, scope, revoked}` built from the control plane,
  refreshed periodically (for example every 30 to 60s) and on a push signal. It
  computes `sha256` of the presented key (the same `hash_key` transform as
  `infra/gateway/src/gateway/auth.py`) and looks up locally. No Postgres or
  gateway call sits in the request path.
- **Refresh source.** A new read-only gateway endpoint the proxy polls:

  | Method | Path | Purpose | Auth |
  | --- | --- | --- | --- |
  | GET | `/v1/edge/keys` | Return active key hashes to tenant-UUID/scope mappings for the edge cache. Metadata only, never raw keys. | proxy service token |

  The response is a list of `{key_hash, tenant_id, scope, revoked_at}` (only the
  SHA-256 hash, never a raw or reversible token, consistent with Initiative 1
  §11). The proxy caches it and diffs on refresh. Revoked keys (`revoked_at`
  set) are removed from the cache on the next refresh, so revocation propagates
  within the refresh interval; document that bounded window as the revocation SLA
  for the proxy path.
- **Fail-closed on unknown key.** A key not in the cache is rejected (`403`) when
  `EDGE_PROXY_REQUIRE_TENANT=true` (the hosted default), never forwarded
  unauthenticated. A brand-new key created seconds ago may miss the cache until
  the next refresh; the dashboard snippet copy notes first events can take up to
  one refresh interval to attribute (§9). This is honest and bounded, not a
  fabricated success.
- **Auth to the refresh endpoint.** The proxy authenticates to the gateway with a
  server-only service token, the same posture Initiative 1 uses for the web
  server (`GATEWAY_SERVICE_TOKEN`, Initiative 1 §6). The proxy never holds a
  human session.

### 6.3 Canonical tenant tag

The proxy's telemetry (`TraceRecord`, `infra/edge-proxy/internal/proxy/trace.go`)
carries `TenantKey` today. For canonical `TenantId = UUID` (Initiative 1,
Decision 4), the proxy must tag emitted telemetry with the **tenant UUID it
resolved from the key** (§6.2), not the key string or any name. When it ships
telemetry to `EDGE_PROXY_TELEMETRY_URL`, the resolved UUID is what lands in
`otel_spans.TenantId`, so proxy traffic and SDK traffic for the same org share
one `TenantId` and one Cost Explorer view. Add the resolved `TenantId` field to
the emitted record (still metadata only; a UUID is not customer content).

## 7. Account attribution without the pre-hash burden

Be honest about the constraint: the account hash exists so a raw customer id
cannot be reversed or joined across tenants, and cannot leave the customer's
process. The two paths differ, and the burden is not fully removed for the proxy.

- **SDK path: automatic.** With the HMAC bootstrap (§3.2), `with_account("acct_...")`
  hashes the raw id in-process under the tenant key and emits only
  `gen_ai.account_id_hash` (+ version). The raw id never leaves the process. Zero
  extra work for the developer beyond setting the account once.
- **Proxy path: pre-hash or land unattributed.** The proxy holds no HMAC key
  (`infra/edge-proxy/README.md`, "The proxy holds no HMAC key and will not hash
  for you"). Hashing a raw id at the proxy would route a raw customer id through
  ai-tally, which is exactly what the hash prevents. So a proxy caller either:
  1. sends a pre-hashed value in `X-Tally-Account-Id-Hash` (64-char HMAC-SHA256
     hex, already supported, stripped before upstream, stored as
     `otel_spans.AccountIdHash`), or
  2. sends nothing and lands in the **unattributed** bucket (empty string, never
     a customer named "unknown", never ranked with real accounts).

  To make option 1 a one-liner rather than a research project, ship a helper that
  computes the hash the same way the SDK does
  (`HmacKeyRegistry.hash_account`, `sdk/python/src/tally/hmac_keys.py`):

  ```python
  # library helper for callers who still have Python at the edge
  from tally import hash_account
  h = hash_account("acct_northwind")   # uses the bootstrapped tenant key
  ```

  and a CLI form (`python -m tally.hash_account acct_northwind`) for scripting.
  The helper still needs the tenant HMAC key, which it fetches via the same
  bootstrap (§3.2) under the org's ingest key, so the raw id is hashed on the
  customer's own machine, never at the proxy.

**Stated tradeoff:** the SDK path removes the hashing burden entirely; the proxy
path does not. A pure zero-code proxy user who wants per-customer attribution must
hash the id themselves (helper provided) or accept unattributed cost. We do not
claim otherwise. Cost per model and per feature works on the proxy path with no
hashing at all; only the per-customer dimension carries this constraint.

## 8. Pricing, zero setup

- **Who prices.** The gateway prices authoritatively on ingest:
  `enrich_cost(span, catalog, tenant_id=batch.tenant_id)` over
  `app.state.catalog = seed_catalog()`
  (`infra/gateway/src/gateway/app.py`). This holds for both paths, because both
  paths become spans the gateway ingests. Per-tenant catalog overrides
  (enterprise rates) apply where present (`tally.pricing`), otherwise the public
  seed catalog. The org configures nothing.
- **SDK path double-checks locally.** The SDK also computes cost in `build_span`
  from the bundled catalog so a span carries an estimate immediately; the gateway
  re-enriches authoritatively and records catalog drift when the SDK's version
  differs (`app.py` catalog-drift logging). The authoritative number is the
  gateway's.
- **Proxy path token prerequisites.** Pricing needs token counts. The proxy's
  `extractMeta` already reads model + prompt/completion tokens from non-streaming
  responses per provider (`infra/edge-proxy/internal/proxy/provider.go`). Two
  known gaps, tracked upstream, gate full proxy pricing:
  - **CTO-40 (SSE token reconstruction).** Streaming responses do not carry a
    usage block mid-stream; the proxy must reconstruct or read the terminal usage
    event to price streamed calls. Until then, streamed proxy calls land with
    model but null tokens (honest blank), not a guessed cost.
  - **CTO-41 (response extractors).** Broader / normalized extraction across
    provider response shapes.

  These are proxy-core follow-ons (both are called out as out-of-core in
  `infra/edge-proxy/README.md`). This spec depends on them for streamed proxy
  pricing and does not re-implement them. Non-streaming proxy calls price today
  from the metadata the proxy already extracts.
- **Honest blanks.** Where tokens are unknown (a stream without usage, a response
  past the scan cap), cost is null with a reason on hover, never zero and never
  guessed (CLAUDE.md invariant).

## 9. First-data onboarding

Delivered in the dashboard (Initiative 1 owns the dashboard shell and the key
creation UI; this initiative adds the onboarding surface around it).

- **Snippet generator, shown right after key creation.** When an admin mints a
  key (Initiative 1 §7 shows the raw token once), render a copy-paste snippet
  per path and language, with the real key inlined into that one-time view:
  - Proxy: shell / env-var snippet setting the provider base URL to the hosted
    proxy and `X-Tenant-Key` to the key, for OpenAI and Anthropic.
  - SDK: `pip install tally` + `tally.init("tally_sk_live_...")` for Python, with
    a note that provider calls are then auto-instrumented.
  The generator is templated per provider and language; no secret is stored to
  render it later (the key is only in the one-time creation view, consistent with
  Initiative 1 §11).
- **Live "we received your first event" state.** A lightweight check for any span
  for the tenant. Implementation: a ClickHouse existence probe scoped to the
  tenant UUID (`SELECT 1 FROM otel_spans WHERE TenantId = {tenant:String} LIMIT 1`,
  using the canonical UUID from `getTenant()`, Initiative 1 §7). The onboarding
  panel polls it and flips from "waiting for your first event" to "connected,
  view Cost Explorer" when a row appears. It reports the honest state: waiting,
  or connected; it never claims data that is not there.
- **Refresh-window note.** Because a brand-new key may miss the edge cache for up
  to one refresh interval (§6.2), the proxy snippet notes first events can take a
  few seconds to attribute. The SDK path has no such delay (the gateway resolves
  the key directly per request).

## 10. Depends on (handoffs from Initiative 1)

Exact handoffs consumed here:

1. **Per-org ingest keys.** `api_keys` rows minted as `tally_sk_live_...`,
   SHA-256 hashed, scoped, revocable (Initiative 1 §5, §2 Decision 5). This
   initiative sends that key as SDK bearer and proxy `X-Tenant-Key`. It mints no
   keys of its own.
2. **Canonical `TenantId = UUID`.** Ingest already stamps the key's tenant UUID
   (`result.tenant_id`), and Initiative 1 reconciles seed / backfill / dashboard
   onto the UUID (Initiative 1 §8). The SDK omits the tenant (key decides) and the
   proxy tags telemetry with the resolved UUID (§6.3), so both agree.
3. **Per-org HMAC key set.** Provisioned per org at org creation into
   `tenants.hash_salt_kek_ref` as a Secret Manager / KMS reference (Initiative 1
   §4). The new `/v1/tenant/hmac-key` endpoint (§3.2) reads the active material
   through that reference.
4. **Service-token control-plane auth.** `GATEWAY_SERVICE_TOKEN` (Initiative 1
   §6) gates the new `/v1/edge/keys` refresh endpoint (§6.2). The
   `/v1/tenant/hmac-key` endpoint is instead authenticated by the ingest key
   itself, because the SDK holds no service token.
5. **Dashboard shell + key creation UI.** The snippet generator and first-data
   panel (§9) attach to Initiative 1's key-creation flow and `getTenant()`.

If Initiative 1 has not landed, this initiative cannot ship: there are no real
per-org keys to connect with. That is the dependency, stated plainly.

## 11. Invariants respected

Cross-checked against CLAUDE.md:

- **Honest under uncertainty.** Unknown tokens and unknown cost render null with a
  reason, never zero or guessed (§4.3, §8). A failed HMAC bootstrap yields
  unattributed accounts, never a raw id (§3.3). The first-data panel shows the
  real state only (§9).
- **No bodies in telemetry.** The SDK emits counts / hashes only; the proxy stays
  metadata-only and byte-for-byte pass-through (`TraceRecord` has structurally no
  body field, `infra/edge-proxy/internal/proxy/trace.go`). No prompt or
  completion is added anywhere.
- **Identifiers by hash, credentials by reference.** Account / user ids are
  HMAC-SHA256'd in the customer process under the per-tenant key (§3, §7). Raw ids
  never leave the process. `tenants.hash_salt_kek_ref` stays a KMS reference; the
  HMAC endpoint returns one tenant's active key material to that tenant's own
  process only, never a raw provider key and never another tenant's key (§3.2).
  Provider API keys ride in-flight only through the proxy and are never persisted
  (`infra/edge-proxy/README.md`).
- **Money is integer micro-USD.** Cost is computed as integer micro-USD with
  `Decimal` rate math via the existing pricing path
  (`tally.pricing.compute_cost_micro_usd`, `enrich_cost`). No float dollars are
  introduced.
- **The SDK never raises into the customer code path.** `init` and every wrapper
  keep the provider call outside the safety boundary and run only span-building /
  transport inside `safe_block` (§4.5). A bug never changes the caller's result.
- **Control-plane writes via the gateway.** No new direct-to-Postgres access. The
  two new endpoints are gateway endpoints; the SDK and proxy call the gateway, and
  the web app is unchanged in how it writes.

## 12. Open questions

1. **HMAC key export policy.** Is returning active key material to the SDK
   (§3.2) acceptable in the strictest deployments, or should high-security tenants
   pin proxy-only (no client-side hashing, accept the pre-hash burden)? A
   per-tenant flag to disable `/v1/tenant/hmac-key` may be warranted.
2. **Edge cache refresh mechanism.** Poll interval vs. a push channel (for
   example a pub/sub or a version-stamped long-poll) for `/v1/edge/keys`. Poll is
   simplest and bounds the revocation window; push tightens it. Which for P1?
3. **Streamed proxy pricing timeline.** Full proxy pricing for streamed calls
   depends on CTO-40 / CTO-41. Do we ship P1 proxy pricing for non-streaming only
   and mark streamed calls as token-unknown until those land?
4. **Anthropic base-URL ergonomics.** The Anthropic SDK's base-URL override and
   header conventions differ from OpenAI's; confirm the exact one-line env the
   snippet generator emits for the Anthropic proxy route.
5. **Async transport under serverless.** A daemon thread + `atexit` flush may not
   drain reliably in short-lived serverless invocations. Do we add an explicit
   `tally.flush()` requirement (or a sync-on-exit mode) for that audience?
6. **Third provider (Gemini).** Gemini extraction exists in the proxy; wiring it
   into the hosted router and an SDK instrumentor is a fast-follow. In or out of
   the P1 hosted endpoint?

## 13. Phasing

### P1: hosted multi-provider proxy with fast key resolution + pricing

Scope: the route table (`EDGE_PROXY_ROUTES` / `EDGE_PROXY_ROUTE_MODE`, §6.1)
serving OpenAI + Anthropic from one hosted endpoint with single-origin
back-compat; the edge key cache and `/v1/edge/keys` refresh endpoint with
fail-closed unknown-key handling (§6.2); canonical-UUID tagging on proxy
telemetry (§6.3); non-streaming proxy pricing from the built-in catalog (§8).

Done when: an org points `OPENAI_BASE_URL` (or the Anthropic base URL) at the
hosted proxy with its real `tally_sk_live_` key and sees non-streaming calls land
in Cost Explorer under its tenant UUID, priced from the catalog, with revoked keys
rejected within the refresh window; `go build ./...`, `go test ./...`, and
`gofmt -l .` (nothing listed) pass for the edge proxy, and gateway `pytest` /
`ruff` pass for the new refresh endpoint.

### P2: SDK `tally.init` auto-instrumentation + HMAC bootstrap + background batcher

Scope: `tally.init` (§3) with tenant-free operation; `patch_openai` /
`patch_anthropic` monkeypatching covering sync, async, and streaming with
never-raise (§4); the `AnthropicInstrumentor`; `/v1/tenant/hmac-key` bootstrap
with in-process caching and the unattributed fallback (§3.2, §3.3); the
background batching transport with retry and backpressure to `/v1/batches` (§5);
the `hash_account` helper + CLI (§7).

Done when: `pip install tally` then `tally.init(key)` auto-instruments an
unmodified `openai` and `anthropic` app (sync + async + streaming), spans reach
`/v1/batches` on a background thread without blocking or raising, accounts hash
in-process under the bootstrapped key (and land unattributed, never raw, when the
bootstrap fails), and SDK `pytest` / `ruff` pass with new tests for the wrappers,
the bootstrap, and the never-raise boundary.

### P3: first-data onboarding + snippet generator

Scope: the per-path, per-language snippet generator shown at key creation, and
the live "we received your first event" ClickHouse existence probe on the tenant
UUID (§9), attached to Initiative 1's key-creation flow and `getTenant()`.

Done when: right after minting a key, an admin can copy a working proxy or SDK
snippet with the real key, and the onboarding panel flips from waiting to
connected when the first span for the tenant lands; `web` typecheck, lint, and
vitest pass, introducing no new failures beyond the two known
ClickHouse-reachability cases (CLAUDE.md).

## 14. File-level change list

SDK (`sdk/python/src/tally/`):

- `init.py` (new): `init()`, `flush()`, `uninstrument()`, process-global client.
- `transport.py` (new): background batching exporter (§5).
- `instrumentation/patch.py` (new): `patch_openai()`, `patch_anthropic()`,
  reversible, idempotent.
- `instrumentation/anthropic.py` (new): `AnthropicInstrumentor` +
  `instrument_anthropic_create` mirroring `instrumentation/openai.py`.
- `instrumentation/base.py`: add async and streaming wrapper variants alongside
  `wrap_create` (sync only today).
- `instrumentation/openai.py`: add Responses API and embeddings extraction.
- `hmac_keys.py`: add a `KeyMaterialProvider` that loads material from the
  `/v1/tenant/hmac-key` response and caches with a TTL.
- `__init__.py`: export `init`, `flush`, `hash_account`.
- `README.md`: document the one-line path; keep the manual path.
- `tests/`: `test_init.py`, `test_patch_openai.py`, `test_patch_anthropic.py`,
  `test_transport.py`, `test_hmac_bootstrap.py`, streaming + async + never-raise
  cases.

Gateway (`infra/gateway/src/gateway/`):

- `app.py`: register `GET /v1/tenant/hmac-key` (ingest-key auth) and
  `GET /v1/edge/keys` (service-token auth).
- new store / handler modules for the two endpoints following the existing store
  pattern (`tenant_lookup.resolve_tenant_uuid`, service-token gate from
  Initiative 1 §6).
- `tests/`: endpoint auth, tenant isolation, metadata-only `/v1/edge/keys`
  (never returns raw keys).

Edge proxy (`infra/edge-proxy/`):

- `internal/config/config.go`: add `EDGE_PROXY_ROUTES` / `EDGE_PROXY_ROUTE_MODE`,
  keep `EDGE_PROXY_UPSTREAM` fallback.
- `internal/proxy/`: routing layer (host / path to origin+provider); edge key
  cache with periodic refresh from `/v1/edge/keys`; resolve `X-Tenant-Key` to
  tenant UUID; stamp `TenantId` on `TraceRecord`.
- `internal/proxy/trace.go`: add resolved `TenantId` field.
- tests: routing, cache hit / miss / revoked, fail-closed, p99 budget unchanged.

Web (`web/`): snippet generator component and first-data onboarding panel,
attached to Initiative 1's key-creation flow; ClickHouse existence probe scoped to
the tenant UUID.
