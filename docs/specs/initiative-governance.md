# Initiative: AI governance

Status: draft spec, for review. Not build-ready: section 14 lists the decisions still open. Owner: platform. Ticket: TODO (file the umbrella ticket before implementation).

This is a design spec. It defines the decisions, the settings model, the policy model, the enforcement pipeline, the APIs and the phasing. It does not contain the implementation.

It builds on, and does not re-litigate:

- Initiative 1 (`docs/specs/initiative-1-orgs-and-access.md`): Clerk owns dashboard identity, the org is the tenant, ai-tally owns per-org API keys, the canonical tenant id is the UUID.
- Initiative 2 (`docs/specs/initiative-2-one-step-connect.md`): the hosted edge proxy and `tally.init`.
- `docs/routing-intelligence-scope.md`: what is measurable today, and the no-bodies analysis this spec relies on in section 6.
- The per-organization proxy switch (merged in #385), which is the working template for the settings model in section 3.

## 1. Summary, goals, non-goals

### Summary

ai-tally today tells a team what its AI costs and whether it pays for itself, after the fact. Governance moves ai-tally into the request path as a policy layer: before an AI call runs, ai-tally's rules decide whether it may run, on which model, with what prompt, within what budget; after it runs, whether its output is acceptable, and whether to escalate.

The architecture is **central policy, local enforcement**. Policies are authored, versioned and learned in ai-tally's control plane. Decisions are made at the edge, inside the customer's request path (the SDK, or an edge proxy), against a cached, signed policy bundle. Only decisions, verdicts and hashes come back to ai-tally, never prompts or completions.

Every part of it is **optional and settings-driven**. An organization can have governance entirely off, on in shadow mode (evaluate and record, never act), or enforcing, and each capability has its own mode.

### Goals

1. An org admin can turn governance on or off for the organization, and set each capability to off, shadow or enforce, from the dashboard.
2. Shadow mode shows exactly what enforcement would have done, with enough evidence to decide whether to enforce, before anything changes production behavior.
3. Deterministic checks add under 2 ms at p99, measured end to end per enforcement point, on top of the proxy's existing 3 ms budget. Network round trips (Redis, a synchronous lease request) and request-body inspection are excluded from that figure and have their own budgets (section 5.1).
4. Model routing decisions come from measured cost and quality per task type, not from a model reasoning about each call.
5. Every decision is attributable to a policy version and is auditable, and every settings change records who made it.
6. Prompts and completions never reach ai-tally's storage by default. The one deployment where they reach ai-tally's infrastructure at all is the hosted proxy: there they transit it, and, when a capability that reads request bodies is on, are inspected in memory. Both are disclosed, and inspection needs its own confirmation (section 3.6).
7. A control-plane outage never causes ai-tally to start blocking a customer's traffic.

### Non-goals

- **A general LLM gateway.** Provider failover, retries, caching of responses and key vaulting beyond what Initiative 2 has are not in scope. ai-tally's differentiation is decisions grounded in measured cost and quality per customer and feature; it does not compete on gateway plumbing.
- **Content moderation as a product.** PII, secret and injection detection are policy inputs. ai-tally does not become a safety classifier vendor.
- **Serving cached answers.** Memoization needs retained answers and collides with the no-bodies invariant (routing-intelligence scope, Layer 6). Repeat-rate reporting from hashes is in scope; serving is not.
- **Per-end-user policy** inside a customer's product. Scoping is org, then feature tag (section 3.4).
- **Compliance certification** (EU AI Act, SOC 2 controls mapping). The audit log is designed to support that later; claiming it is not in scope.

## 2. Decisions

**D1. Central policy, local enforcement.** Policies live in the control plane; decisions run where the call runs. A remote "ask ai-tally" call per LLM request is rejected: it adds a network round trip to every call, makes ai-tally's uptime the customer's uptime, and requires sending the prompt out to check it.

**D2. One policy semantics, four enforcement points.** The same policy bundle is evaluated by: the hosted edge proxy, a customer-run edge proxy (shared in-VPC gateway or sidecar), and the Python SDK. Evaluation logic is specified once (section 5) and conformance-tested across the Go and Python implementations against shared fixtures, the same way PR #377 ties the SDK and proxy token parsing to shared fixtures.

**D3. Settings-driven at four layers.** Deployment, plan, org master switch, per-capability mode (section 3). Each layer can only narrow what the layer above allows.

**D4. Off means pass-through, not refusal.** With governance off, traffic is metered exactly as today. This is the opposite of the proxy switch in #385, where off refuses, because there using the proxy is itself the opt-in. Here, the opt-in is to being governed.

**D5. Fail toward the least disruptive safe state.** An unreadable or stale setting can only lower a mode: an org last known to have governance on is capped at shadow, and an org whose settings were never loaded is off, because nothing says it opted in (D4). Nothing unknown ever resolves to enforce. The one exception is a kill switch a person activated, which is sticky (section 6.7).

**D6. Deterministic inline, probabilistic out of line.** No LLM runs on the call path. LLM judges run asynchronously on samples (section 6.6), and their own error rate is measured and shown next to every quality figure they produce.

**D7. Prompts stay in the customer's boundary.** Checks that need a prompt run where the prompt already is. A hosted quality judge exists only as an explicit opt-in tier with its own consent, retention and export exclusion, following the replay carve-out precedent (`tenant_replay.CANDIDATE_RESPONSE_RETENTION_CONSENT`).

**D8. Extend the guardrails model, do not replace it.** `tenant_guardrails` (0006) already has rule kinds, a per-rule `state` of `enabled | shadow | disabled`, and an audit table `tenant_guardrail_changes`. The SDK already has enforcement actions `observe | warn | graceful | hard_stop` (`sdk/python/src/tally/guardrails.py`) and per-rule verdict span attributes. Governance generalizes these; existing rules keep working unchanged.

**D9. The proxy never rewrites request bodies in V1; proxy routing is recommend-only.** An earlier draft proposed letting the proxy rewrite the request's `model` field as a "scoped exception" to its invariant that bodies are forwarded byte for byte. Review rejected that, correctly: it is a change of network posture, not an exception.

- A different model id changes the payload length, so `Content-Length` must be recomputed, or the request re-framed as chunked.
- Rewriting safely means buffering the whole request and parsing it, where today the proxy streams requests untouched (`countingReadCloser` in `proxy.go` only counts bytes). Re-serializing a parsed tree also changes bytes the policy never meant to touch.
- **Parser differentials** open a smuggling path: a body with two `model` keys can be read as an allowed model by the proxy's parser and as a different model by the provider's. Go's `encoding/json` keeps the last duplicate; a provider may keep the first.
- Gemini carries the model in the URL path, not the body, and some endpoints take multipart bodies, so "rewrite the model field" is not even one operation.

So in V1 routing, model rewriting and redaction are **SDK-only**, where the SDK builds the request and nothing is re-parsed. The proxy blocks or records a recommendation. Revisiting proxy rewriting (not before P4) requires all of: D11's strict parser, splicing only the validated value's byte range instead of re-serializing, recomputing `Content-Length`, a request size cap, and its own measured latency budget.

**D10. Shadow first, always.** Every new capability ships in shadow before enforce exists for it, and shadow is the first mode an admin can move a capability to. Shadow is never a default a capability starts in: turning the org switch on enables nothing by itself (section 3.5).

**D11. Request inspection in the proxy is opt-in, bounded, and uses one strict parse.** D9's analysis applies to reading a request, not only to rewriting one. `model_policy` and `prompt_policy` in the proxy must read the request body, which the proxy does not do today. So:

- The proxy buffers a request **only** when some capability that reads the body is in shadow or enforce for that tenant. Otherwise requests keep streaming untouched.
- Buffering is capped at 1 MiB, the same bound as the existing response tee. Over the cap, the capability's `oversize` param decides: `skip_and_flag` (default) or `block`.
- The body is parsed once, **strictly**: duplicate keys at any depth, invalid UTF-8 and trailing data are parse failures, so the value a policy evaluates is the only value the provider can see. A parse failure is `parse_rejected`: blocked under enforce, recorded under shadow (section 5.3).
- The decision and the forwarded request come from the same buffered bytes, which are forwarded unmodified (D9).
- Buffering and parsing have their own line in the latency budget (section 5.1), measured before P1 rather than assumed.

## 3. Settings model

This section is the on/off requirement.

### 3.1 Layers

| Layer | Who decides | Where it lives | Effect when off |
|---|---|---|---|
| 1. Deployment | Operator | Whether governance components run (proxy flag, SDK version) | No evaluation possible |
| 2. Plan | ai-tally billing | `tenants.plan` plus the subscription model (roadmap) | Settings page shows governance as unavailable on this plan |
| 3. Org master switch | Org admin | `tenant_governance_settings.enabled` | Pass-through metering (D4) |
| 4. Capability mode | Org admin | `tenant_governance_capabilities` per capability | That capability is not evaluated |

### 3.2 Capability modes

Each capability (section 4.1) has one of:

- **off**: not evaluated.
- **shadow**: evaluated; the decision is logged with `would_act = true` where it would have acted; the call proceeds untouched.
- **enforce**: evaluated and acted on, using the capability's configured action (section 4.1).

These map onto the existing `tenant_guardrails.state` values (`disabled | shadow | enabled`) so the guardrail rules become capability-scoped rules without a data migration of their meaning.

**Existing rules already have two mode fields, so the mapping needs a precedence.** A guardrail rule has the control-plane `state` above and the SDK's `GuardrailConfig.mode` (`observe | warn | graceful | hard_stop`). A rule that is `enabled` with `mode = observe` behaves as shadow. The effective mode of a rule is therefore the **minimum** of the capability's mode, the rule's `state`, and its SDK mode, where `observe` counts as shadow and `warn`, `graceful` and `hard_stop` are enforce actions. That one value is what decisions record and what the dashboard and enforce confirmation report, so they cannot disagree with what the SDK does.

### 3.3 Resolution

The effective mode for one capability on one call:

```
effective(capability, call) =
    if kill_switch.active(scope_of(call)):              KILL      # sticky only when a person activated it (6.7)
    if not deployment_supports(capability):              off
    if not plan_includes(capability):                    off
    known = current bundle, else last cached bundle, else none
    if known is none:                                    off       # never loaded: nothing says the org opted in (D4)
    if not known.org_enabled:                            off       # checked BEFORE any degraded branch
    mode = known.capability_mode(capability)            # a missing capability row is off (3.5)
    mode = tighten(mode, known.override(call.feature_tag, capability))   # 3.4, P2+
    if known is not current, or stale beyond max_staleness:   mode = min(mode, shadow)
    return mode
```

`min` orders `off < shadow < enforce`. The org switch is read before the degraded branches on purpose: an earlier draft capped unreadable settings at shadow first, which evaluated (and, under D11, inspected) the traffic of orgs that had turned governance off whenever a proxy restarted during a control-plane outage. "Unreadable" and "stale" can only lower a mode, never raise it, and never move an org that was off into shadow.

### 3.4 Scoping

- **P0 to P1:** org-wide only.
- **P2 onward:** per-feature-tag overrides (`FeatureTag` is already a first-class dimension on every span), so a team can enforce on `checkout` while the rest of the org stays in shadow.
- **Overrides only tighten, by default.** The feature tag is set by the caller (`X-Tally-Feature-Tag` on the proxy, `start_trace(feature_tag=...)` in the SDK). An override that loosened policy would let a buggy service or a prompt-injected agent step around enforcement by putting a looser feature's tag on its calls. So `tighten(mode, override)` in section 3.3 takes the stricter of the two. Loosening is allowed only for a feature bound server-side, and the resolution rule is exact, not left to implementation:
  - **The policy tag comes from the key when the key is feature-scoped.** A request authenticated with a key bound to `checkout` is governed as `checkout`, whatever `X-Tally-Feature-Tag` says. A header naming a different feature is ignored for policy and recorded as `tag_mismatch`.
  - **With an org-level key, the header tag is used for attribution and for tighten-only overrides, never for a loosening override.** A request with an org key and `X-Tally-Feature-Tag: loose_feature` resolves to the org policy, tightened by any stricter `loose_feature` settings, and never loosened by them.
  - **This is enforced where the decision is made: the proxy or the SDK, not the gateway.** The gateway never sees per-request headers, only decisions. The bundle marks which overrides loosen, and the key feed carries each key's bound feature tag, so an enforcement point can resolve the tag without a network call.
  - **A conformance fixture pins it** in both Go and Python: an org key plus a header naming a loosening feature must resolve to org policy, and a feature-scoped key plus a mismatching header must resolve to the key's feature.
  - The settings UI labels a loosening override as such and lists the keys bound to it.
- **Not planned:** per-end-user overrides (non-goal).

### 3.5 Schema (Postgres, new; next free migration numbers after #385's 0033)

```sql
CREATE TABLE tenant_governance_settings (
    tenant_id     UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    enabled       BOOLEAN NOT NULL DEFAULT false,
    max_staleness_seconds INTEGER NOT NULL DEFAULT 300 CHECK (max_staleness_seconds BETWEEN 30 AND 86400),
    updated_by    TEXT CHECK (updated_by IS NULL OR length(updated_by) <= 128),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenant_governance_capabilities (
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    capability    TEXT NOT NULL CHECK (capability IN
                    ('spend','model_policy','prompt_policy','output_policy','routing','quality_judge')),
    feature_tag   TEXT NOT NULL DEFAULT '',          -- '' is the org default (section 3.4)
    mode          TEXT NOT NULL DEFAULT 'shadow' CHECK (mode IN ('off','shadow','enforce')),
    params        JSONB NOT NULL DEFAULT '{}'::jsonb, -- capability config, section 4.1
    updated_by    TEXT CHECK (updated_by IS NULL OR length(updated_by) <= 128),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, capability, feature_tag)
);

-- Audit: every change to either table, before and after, like tenant_guardrail_changes (0006).
CREATE TABLE tenant_governance_changes (
    change_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor         TEXT CHECK (actor IS NULL OR length(actor) <= 128),
    target        TEXT NOT NULL,                     -- 'settings' | 'capability:<name>[:<feature>]' | 'kill_switch'
    before        JSONB,
    after         JSONB,
    bundle_version BIGINT,                           -- the bundle this change produced
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**A capability row that does not exist reads as `off`, whether the org switch is on or not.** The row is created when an admin first moves that capability to shadow; its `mode` column default is only used at that moment. An earlier draft read a missing row as shadow once the org switch was on, which meant turning governance on immediately put `model_policy` and `prompt_policy` in shadow for every tenant, and under D11 the proxy would start buffering, parsing and scanning every request on the first click. Turning the org switch on therefore changes nothing until a capability is enabled, and the settings page marks which capabilities read request bodies before an admin enables them.

### 3.6 Who may change what

- Reading settings: any org member.
- Changing settings, modes, policies: `org:admin`, enforced in the web server as for API keys and the proxy switch (#385), because the gateway sees only the service token.
- Moving any capability from shadow to **enforce** additionally requires a confirmation that shows the last 7 days of shadow decisions for it (section 10). This is a product guard, not a permission.
- **Enabling a body-reading capability for traffic that uses the hosted proxy** requires its own confirmation, because prompts are then inspected in ai-tally's process memory, not only carried through it. The hosted-proxy switch shipped in #385 says "prompts and responses are forwarded, never stored"; while a body-reading capability is on for that org, its copy must also say they are inspected.

## 4. Policy model

### 4.1 Capabilities

| Capability | Inputs at the edge | Decides | Enforce actions | Builds on |
|---|---|---|---|---|
| `spend` | estimated cost, counters | whether the call fits budget | `warn`, `downgrade` (route cheaper), `block`, `truncate` (streaming only, 5.2) | `tenant_budgets` (0026), SDK `cost_cap` |
| `model_policy` | requested model, provider | allowed, deprecated, or disallowed model | `block`; `rewrite` SDK-only (D9) | `model_deprecation` rule kind |
| `prompt_policy` | template id and version, variables, retrieved context, tool outputs | approved template, PII, secrets, injection signals | `redact` (SDK only), `block` | `pii_gate` rule kind |
| `output_policy` | response, requested schema | schema adherence, refusal | `retry`, `escalate`, `flag` | `FormatAdherenceEvaluator`, `RefusalEvaluator` |
| `routing` | task type (feature tag), routing table | which model to call | `route`, `cascade`: SDK-only in V1 (D9); proxy records a recommendation | replay + eval, `wrong-sized-model` detector |
| `quality_judge` | sampled logged calls, criteria | quality score and verdict | none inline; feeds routing and reports | `CorrectnessEvaluator` |

`redact`, `rewrite`, `route` and `cascade` are SDK-only in V1 because the proxy never rewrites request bodies (D9). Proxy capabilities that read the body follow D11.

### 4.2 Policy bundle

The control plane compiles settings, capability params, routing tables and the rule set into one bundle per tenant:

```json
{
  "tenant_id": "5a16edc0-9d8a-43e4-945d-1344993e50d5",
  "version": 42,
  "issued_at": "2026-09-12T18:00:00Z",
  "max_staleness_seconds": 300,
  "enabled": true,
  "capabilities": {
    "spend":         { "mode": "enforce", "action": "downgrade", "budgets": ["b_monthly_org"] },
    "model_policy":  { "mode": "shadow",  "allow": ["claude-haiku-4-5", "gpt-5-mini"], "deny": [] },
    "prompt_policy": { "mode": "shadow",  "scans": ["pii", "secrets", "injection"], "templates": { "support.reply": ["v3", "v4"] } },
    "routing":       { "mode": "off" }
  },
  "overrides": { "checkout": { "spend": { "mode": "enforce", "action": "block" } } },
  "routing_tables": { "summarize": "sha256:9f2c...", "support.reply": "sha256:41ab..." },
  "signature_pack": { "id": "sigpack-2026-09-10", "sha256": "c07e..." }
}
```

Each artifact is served with a small **signed manifest**, not a signature over the document itself:

```json
{
  "tenant_id": "5a16edc0-9d8a-43e4-945d-1344993e50d5",
  "kind": "settings_bundle",
  "version": 42,
  "sha256": "e3b0c442...",
  "expires_at": "2026-09-13T18:00:00Z",
  "key_id": "gov-2026-09",
  "sig": "Ed25519 over the manifest bytes"
}
```

- **Versioning:** a monotonically increasing integer per tenant. Every decision records the version it was made under.
- **Signing: a manifest over the exact bytes.** An earlier draft signed "the canonical JSON" of the bundle with a public key built into the SDK and proxy. Review found three problems, and the manifest above fixes all three:
  - **No canonicalization.** "Canonical JSON" named no scheme, and Go and Python serialize JSON differently, so the same bundle could verify on one side and fail on the other. The manifest carries the SHA-256 of the bytes as served. An enforcement point hashes what it received, checks it against the manifest, and only then parses it. No re-serialization is ever involved.
  - **The KMS size limit.** AWS KMS supports Ed25519 keys (`ECC_NIST_EDWARDS25519`), but signs raw Ed25519 messages of at most 4,096 bytes, far under a 64 to 256 KiB bundle. Its pre-hash variant (Ed25519ph) avoids the limit but is not supported by every verification library. A manifest is well under the limit and uses plain Ed25519, which Go's `crypto/ed25519` and Python's `cryptography` both verify.
  - **Rotation.** A key built into the SDK changes only when every customer upgrades. Instead, the SDK and proxy ship a long-lived **root** public key, and fetch a signed set of current signing keys from `GET /v1/governance/keys`, verified against the root. Signing keys rotate by `key_id` without a client release; the root rotates only with a release, and rarely.
  - The signing keys are held by reference in KMS or Secrets Manager (credentials-by-reference invariant). The gateway has no signing facility today, so this is new work (section 16).
  - An edge rejects an artifact whose manifest does not verify, whose hash does not match, or whose manifest has expired, and keeps its last good artifact.
- **Two kinds of artifact, not one bundle.** Review pointed out that one 256 KiB bundle cannot hold allowlists, routing tables and prompt-injection signatures together, and comprehensive injection heuristics alone run to hundreds of KiB. So:
  - **Settings bundle** (per tenant, small, changes often): settings, capability params, allowlists, and *references* to routing tables and the signature pack by content hash. Target under 64 KiB; hard cap 256 KiB.
  - **Routing tables** (per tenant, per task type): fetched by hash only when the reference changes.
  - **Signature pack** (global, versioned by ai-tally, changes rarely): PII, secret and injection patterns. Megabytes are acceptable. Fetched by hash, cached on disk where the enforcement point has a disk, signed separately.
- **No delta protocol in V1.** Content addressing already gives "fetch only what changed" at artifact granularity, which is where the size is. A byte-level delta protocol adds a second correctness surface for little gain until a single artifact is both large and changes often.
- **Patterns must be linear-time.** A pack runs on attacker-influenced input on the call path, so a backtracking regex is a denial-of-service lever. Every pattern must compile under RE2 semantics: Go's `regexp` already is RE2; the Python SDK must use an RE2 binding (`google-re2`), not the standard `re` module it uses today (`redaction.py`, `evals.py`). A pattern that is not RE2-compatible is rejected when the pack is published, not discovered at the edge.
- **RE2 has no lookarounds, so filtering moves out of the pattern.** Much secret detection written for backtracking engines uses lookaheads and lookbehinds to avoid matching UUIDs, hashes and markup. Under RE2 that job splits into two stages:
  - **Match** with an RE2 pattern, deliberately broad.
  - **Filter each match** in code: a Shannon-entropy threshold, allowlists by shape (UUIDs, hex hashes, HTML and markup tokens, known placeholder and test keys), a keyword proximity window (the match must sit within N bytes of a keyword such as `secret` or `api_key`), and checksum validation where a format has one (Luhn for card numbers, provider key checksums). Filters run only on matches and are bounded by match length, so the whole stage stays linear.
- **Sourcing patterns: adapt, not write from scratch, and not import blindly.** Rules from backtracking-engine libraries often fail to compile under RE2 and cannot be imported as they are. Scanners built on Go's regex engine are different: [gitleaks](https://github.com/gitleaks/gitleaks) rules are Go regular expressions, which cannot use lookarounds, and it handles false positives with allowlists and entropy rather than lookarounds, the same two-stage shape as above. Rules from such sources are the starting point; each is license-checked, reviewed, and must pass the publish gate before it enters the pack.
- **The publish gate:** every pattern compiles under RE2; the pack passes a true-positive corpus (real-format keys and PII, synthetic where needed) and a false-positive corpus (UUIDs, content hashes, HTML, base64 images, minified JavaScript); and the pack's measured false-positive rate is recorded with its version, so a pack that regresses precision is not published.

### 4.3 Distribution

- **Long-running enforcement points** (proxies, SDK in a server process): poll the settings bundle every 30 seconds with `If-None-Match: <version>`, the same outbound-only pattern as the proxy's key feed (#385). Fetch routing tables and the signature pack only when their hash changes.
- **Serverless enforcement points** (SDK in Vercel functions or Lambda): no background threads, so no polling. The bundle is cached in module scope across warm invocations and revalidated inline, with a conditional `GET`, at most once per `bundle_ttl_seconds` (default 30). A cold start fetches it before the first call, which costs one round trip on cold starts only; the signature pack is loaded lazily, only when a capability that scans is active.
- **Kill switch:** a separate, tiny channel (section 6.7), not the bundle.
- **Staleness:** past `max_staleness_seconds` without a successful fetch, every capability is capped at shadow (section 3.3), and the enforcement point reports `bundle_stale` on its decisions.

## 5. Enforcement pipeline

Per call, at the enforcement point. Deterministic steps only; nothing in this path calls an LLM.

```
 1. identify    tenant, feature tag, account hash, agent run    (existing Initiative 2 resolution)
 2. resolve     effective mode per capability                   (section 3.3)
 3. pre-checks  kill switch, spend reservation, model policy,
                prompt policy (scans, template approval)
 4. route       routing table lookup, apply or record           (routing capability)
 5. call        forward to the provider                         (existing proxy / wrapped client)
 6. post-checks output schema, refusal; decide retry or cascade
 7. settle      reconcile spend from the provider's usage block
 8. emit        decision record + span attributes, no bodies    (section 8)
```

### 5.1 Latency budget

| Step | p99 added |
|---|---|
| Mode resolution | 50 µs (in-memory bundle) |
| Spend reservation (local counter) | 100 µs; shared store adds a network hop (section 6.1) |
| Model policy | 20 µs |
| Prompt scans | 1 ms for inputs up to 64 KiB; above that, policy decides `scan_prefix` or `skip_and_flag` |
| Routing lookup | 50 µs |
| Request buffering and strict parse (D11), only when a body-reading capability is active | Measured before P1; target under 1 ms up to 64 KiB. Reported separately from the total below, because it scales with prompt size |
| **End to end, no body-reading capability active** | **under 2 ms**, excluding network round trips |
| End to end, with body-reading capabilities | Budgeted per request-size band after the P1 measurement; not folded into the 2 ms figure |

The per-step figures are design targets for each piece, **not terms of a sum**: p99s do not add, and the end-to-end p99 of a request is not the sum of its steps' p99s. What is measured and reported, per enforcement point and per capability mix, is the end-to-end p99 of added latency (section 17). A Redis call or a synchronous lease request is a network round trip, reported separately with its own p99, because it depends on where the store is, not on the policy.

### 5.2 Streaming

Post-checks that need the whole output run after the stream completes and can only flag or trigger a follow-up.

Spend enforcement **can** act mid-stream, but only under the explicit `truncate` action, never as part of `block`. Many clients treat a closed SSE stream as the end of a normal answer and keep the partial content without an error, so simply closing the stream would turn a budget stop into a silently truncated reply in the customer's product. Under `truncate`, when the output crosses the limit, the enforcement point:

- sends a **provider-shaped terminal error event** first (the error event format each provider's client already raises on), then closes the stream;
- in the SDK, raises a typed `tally.governance.BudgetTruncated` exception that the application can catch;
- records `truncated_by_policy`.

In the proxy this is the one action that changes what a response contains, which D9 and D11 otherwise avoid, so it is available only where a budget explicitly selects `truncate`.

**When truncation can apply at all.** A request that sets `max_tokens` is reserved at prompt estimate plus `max_tokens` (6.1), so its output cannot exceed its reservation, and mid-stream truncation never triggers for it. `truncate` therefore only matters for requests **without** `max_tokens`, reserved at `default_output_reservation`.

**Counting output mid-stream.** The final usage block is the only exact count for most providers, and it arrives at the end. What each provider gives during a stream:

| Provider | Mid-stream signal | How the running count is known |
|---|---|---|
| Gemini | `usageMetadata` repeats on every chunk with running totals | Exact |
| OpenAI | Usage only on the final chunk, and only when the client sets `stream_options.include_usage` | Estimated from emitted content |
| Anthropic | `message_start` reports output so far (about 1); the cumulative count arrives on `message_delta` near the end | Estimated from emitted content |

Where the count is estimated, no tokenizer runs at the edge, consistent with 6.1. The estimate is the character count of emitted content deltas multiplied by a **per-model output ratio**, and its error is measured, not asserted:

- **Calibration.** Every stream that ends with a usage block records the estimate beside the exact count, per tenant and model. The ratio is the running median of that relationship; the error margin `ε` is its measured 99th-percentile relative error.
- **A conservative trigger.** The stream is cut only when even the low end of the estimate crosses the limit: `estimate x (1 - ε) >= limit`. Truncation is therefore never premature at p99. The price is a bounded overrun of up to `ε x limit` on that call, which the settlement corrects in the counters afterwards.
- **No calibration, no truncation.** Until a model has at least 200 measured streams for the tenant, `truncate` is unavailable for it and the budget falls back to `block` on the next call instead, recorded as `truncate_uncalibrated`. An OpenAI client that never sets `include_usage` never produces a usage block, so calibration is impossible and `truncate` stays unavailable; the dashboard says so rather than guessing.
- **The margin is published.** The dashboard shows `ε` per model next to every budget that uses `truncate`.
- Counting emitted characters means the proxy reads response content lengths in memory, never storing content. That is the same posture as D11's request inspection and falls under the same hosted-proxy disclosure (3.6).

### 5.3 Failure modes

| Condition | Behavior | Recorded as |
|---|---|---|
| Control plane unreachable, bundle cached | Use the cached bundle until `max_staleness_seconds`, then cap at shadow | `bundle_stale` |
| No bundle ever loaded | Every capability off: traffic passes through as with governance off (D4, 3.3) | `bundle_missing` |
| Bundle signature invalid | Reject it, keep the last good bundle | `bundle_rejected` |
| Scan input over size cap | Per policy: scan the prefix, or skip and flag | `scan_truncated` / `scan_skipped` |
| Request over the 1 MiB inspection cap (D11) | Per capability `oversize`: skip and flag (default), or block | `request_too_large` |
| Strict parse fails: duplicate keys, invalid UTF-8, trailing data (D11) | Enforce: block. Shadow: proceed and record. A smuggling attempt is never silently evaluated | `parse_rejected` |
| Spend lease or Redis unavailable (section 6.1) | Per `spend.on_unavailable`: `shadow` (default, availability first) or `block` (budget first) | `lease_unavailable` / `counters_unavailable` |
| Lease denied because budget is held in other instances' unexpired leases, not spent (6.1) | Retry once after a short backoff, then per `on_unavailable`; never the budget's `block` action, since the budget is not exhausted | `budget_leased_out` |
| `truncate` requested for a model without calibration (5.2) | No mid-stream cut; the next call is evaluated under `block` | `truncate_uncalibrated` |
| Signature pack missing or hash mismatch | Use the last good pack; if none was ever loaded, cap `prompt_policy` at shadow | `sigpack_stale` / `sigpack_missing` |
| Provider error during a cascade | Return the last successful response, or the original error | `cascade_failed` |
| Evaluation throws | The call proceeds, as if the capability were shadow | `governance_error` |

The last row is the rule behind all of them: a bug in governance must never be the reason a customer's call failed.

## 6. Capabilities in detail

### 6.1 Spend

- **Reservation before the call:** `estimated_prompt_tokens + max_tokens`, priced from the catalog in integer micro-USD.
  - Prompt tokens are estimated from character count with a per-provider ratio, recalibrated weekly from each tenant's own recorded usage. No tokenizer ships to the edge.
  - A request with no `max_tokens` is reserved at the policy's `default_output_reservation`. The request is not modified to add one (the proxy does not mutate bodies).
- **Settlement after the call:** the reservation is replaced by the exact cost from the provider's usage block, which the proxy and SDK already parse (#372, #377, #378). Usage arrives in the response body, not headers.
- **Counters.** Budgets are rolling totals per scope (`tenant`, `feature`, `model`, `layer`, matching `tenant_budgets.scope_kind`). Three tiers, chosen by deployment:
  - **One instance** (the hosted proxy today): exact local counters.
  - **Redis** (the only shared store in V1, hardcoded, no pluggable interface): reserve and settle are single atomic Lua scripts against a key per scope and period. Exact. Serverless functions reuse the client across warm invocations; the per-cold-start connect cost is accepted and measured.
  - **No Redis, multiple replicas: spend leases.** Replicas do not share a counter; they hold leases.

- **Why leases, and not the earlier draft.** The first draft had each replica enforce `budget / N` and sync settled totals every 60 seconds. Review showed the overrun: with $10 left and a spike across 50 replicas, each believes it has room, and together they can spend far past the budget inside the sync window. Partitioning statically ($10 / 50 = $0.20 each) fixes that case but breaks two others: autoscaling changes N while replicas hold stale partitions, so new replicas over-allocate, and uneven traffic blocks a hot replica while idle ones sit on unspent budget.

- **Lease design.**
  - A replica requests a lease from the control plane: a slice of the *remaining* budget, sized `max(min(remaining x lease_fraction, lease_max), min_lease)` (defaults 5%, $1, and `min_lease` equal to the budget's largest recent worst-case reservation), with a 30-second expiry.
  - **A lease smaller than one call is never granted.** Without the floor, leases shrink geometrically toward zero as a tenant nears its budget: at $0.40 left a 5% lease is $0.02, below a single long-context call's reservation, so every call would exhaust its lease and need a synchronous lease request. That is the per-request control-plane round trip D1 rejects, arriving exactly at the budget edge. When `remaining < min_lease` and no other leases are outstanding, the lease request is **denied**, and the call follows the budget's action (`warn`, `downgrade`, `block`), because the budget is effectively spent.
  - **Held is not spent.** When the unleased remainder is below `min_lease` but other leases are still outstanding, the budget is not exhausted; it is sitting in other instances. That denial is `budget_leased_out`, and the call retries once and then follows `on_unavailable` (5.3), never the budget's `block` action, so a hoarding problem never presents as a spent budget.
  - Calls reserve against the local lease. A background refill requests more below 50% remaining; a synchronous request happens only when a lease is exhausted, and **waits at most 50 ms** before `on_unavailable` applies, so a slow control plane can never stall the call path.

- **Scale-to-zero containers hoard leases unless they run in ephemeral lease mode.** Section 7 requires Redis for serverless functions, but containerized platforms that scale to zero (Google Cloud Run, AWS App Runner, Azure Container Apps) look long-running to the SDK and would use ordinary leases. An instance that takes a lease, serves one small call and then goes idle holds the rest of that lease until it expires. Up to `lease_max` per instance, across many idle instances, can starve active ones into `budget_leased_out`. (`min_lease` is one worst-case reservation; the large grants come from the `lease_fraction` and `lease_max` sizing.)
  - **Returning the lease after the response is not enough on these platforms.** With Cloud Run's default request-based billing, CPU is throttled as soon as a response completes, so work scheduled after the response (a post-response hook, a background thread) may not run until the next request arrives ([Cloud Run billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings)). A return that only happens there is a return that often does not happen.
  - **Ephemeral lease mode** is selected when the SDK detects such a platform from its environment (for example `K_SERVICE` on Cloud Run), or explicitly with `TALLY_RUNTIME=ephemeral`. In this mode:
    - leases are sized to in-flight need, one worst-case reservation per concurrent call, not a fraction of the remaining budget;
    - when an instance's in-flight count drops to zero, the unused portion is **returned inline, before the response is written**, while the platform still grants CPU; the return is a single small request, bounded by the same 50 ms cap, and a failed return simply expires;
    - expiry is 10 seconds instead of 30;
    - refills and returns double as heartbeats, and the control plane reclaims any lease not heartbeated within one expiry, so a frozen instance's lease is freed on schedule even if it never returns it.
  - **Redis is still the better answer** for these platforms, because there is nothing to hoard. The SDK logs a one-time recommendation when it detects a scale-to-zero platform running on leases.
  - **The invariant:** the control plane never has outstanding leases totalling more than the remaining budget. Worst-case overrun is therefore bounded by reservation *estimation error* on in-flight calls, not by replica count or traffic shape. Autoscaling is safe, because a new replica gets a lease from what is left rather than a partition of the original.
  - Unused lease budget returns on expiry or settlement, so idle replicas do not strand it.

- **When leases or Redis are unavailable.** This is a real trade-off, and it is made explicit per budget rather than hidden inside D5. `spend.on_unavailable = shadow` (default) keeps traffic flowing and records `lease_unavailable`; spend is then unenforced until the control plane or Redis returns. `on_unavailable = block` refuses calls instead: budget over availability. The dashboard shows which one each budget uses.
- **Source of truth and seeding.** Recorded spend in ai-tally lags real spend: spans are batched, deduplicated only after a merge unless read with `FINAL`, and priced after ingest. An earlier draft reseeded counters from it "on bundle load", which happens on every new bundle version. Overwriting a live counter with that lagging total moves the counter backwards and overruns the budget by the lag; adding it counts spend twice. So:
  - **Seed once per budget period**, from recorded spend up to a *settled watermark* (spans older than the ingest and pricing lag, read with `FINAL`), plus a conservative estimate for the unsettled tail.
  - **After seeding, only reserve and settle move a counter.** A new bundle version never reseeds.
  - **Reconcile in one direction.** A periodic reconcile (every 5 minutes) may *raise* a counter to the recorded total if recorded spend has overtaken it, for example spend from another enforcement point the counter never saw. It never lowers one.
  - **The authority per tier:** the local counter (single instance), the Redis key (Redis), or the control plane's lease ledger (leases). Recorded spend in ClickHouse is an input to seeding and reconciliation, never the live counter.
- **Gate:** `spend` may not be set to enforce until real-traffic verification (#350) has run for the providers the tenant uses (section 12).

### 6.2 Model policy

- Allow and deny lists by model id and provider; deprecation dates, extending the existing `model_deprecation` rule kind.
- `rewrite` maps a disallowed model to its configured replacement. SDK-only in V1; the proxy blocks or records the recommended replacement (D9).

### 6.3 Prompt policy

**The template problem.** A prompt with user input injected hashes differently on every call, so approving a prompt by its full hash cannot work.

- **SDK (full fidelity):** a new API separates template from variables before they are joined.
  ```python
  tally.prompt("support.reply", version="v4", variables={"ticket": ticket_text})
  ```
  The static template is hashed and checked against the approved versions in the bundle. Scans run on the variables.
- **Proxy (reduced fidelity):** the proxy only sees the joined messages. It fingerprints the system message (usually static) plus the message role structure, and runs scans on the rest. The decision records `fidelity = proxy_fingerprint` so the dashboard can say which calls had full template checking. Any of this in the proxy requires request inspection under D11.
- **What gets scanned:** not only user-supplied variables. Prompt injection most often arrives through retrieved documents and tool outputs, so the SDK scans those inputs too when they pass through instrumented retrieval and tool calls.
- **Scans:** PII (pattern and checksum based), secrets (provider key formats, high-entropy tokens), injection signals (pattern set, versioned in the bundle). Only a verdict and the category leave the process.

### 6.4 Output policy

- Schema adherence against the request's declared JSON schema or tool definitions; refusal detection.
- Grows from `FormatAdherenceEvaluator` and `RefusalEvaluator` (`sdk/python/src/tally/evals.py`), which today score replayed outputs only.
- Actions: `retry` (same model, bounded), `escalate` (next model in the cascade), `flag`.

### 6.5 Routing and cascade

- **Routing tables** map a task type (feature tag) to an ordered list of models with measured cost, quality and latency, built offline from replay and evaluation. This is the routing-intelligence scope's P0 and P1: real replay and an outcome signal are prerequisites (section 12).
- **Phases:** recommend (a finding in the dashboard: "switch `summarize` to X, same measured quality, saves $Y"), then route with human approval of the table, then automatic. In V1 only the SDK applies a route; the proxy records the recommendation (D9).
- **Cascade:** try the cheapest model whose measured quality clears the task's floor; on a post-check failure, escalate to the next. Bounded by `max_escalations` (default 1) and by the spend reservation, which must cover the worst case of the cascade, not the first call.
- **Honesty:** a routing claim is only as good as the judge that produced the quality numbers. The routing table carries the judge's measured accuracy, and a recommendation whose quality difference is inside the judge's error is shown as inconclusive, not as a saving.

### 6.6 Quality judge

- **Where it runs:** a worker inside the customer's network, packaged as a container. It pulls evaluation criteria from ai-tally, reads sampled calls from the customer's own store (the SDK's `RESOLVED_CONTEXT` object category, 30-day default retention, in the customer's bucket), calls a judge model on the customer's own provider account, and pushes back only scores, verdicts and counts. Only the SDK writes `RESOLVED_CONTEXT`, so customers who connect through a proxy alone have no sampled calls for the judge to read; for them quality scores are shown as unavailable, not as absent problems. Routing is SDK-only in V1 anyway (D9).
- **Its own accuracy:** measured against a small human-labeled set per task type, and shown alongside every score. Without a labeled set, scores are shown with accuracy unknown, not as if exact.
- **Hosted tier (opt-in):** for customers who accept it, ai-tally hosts the judge. This requires request bodies to reach ai-tally, which the existing replay carve-out does not cover (it covers candidate responses only). It needs its own consent string, retention and export exclusion, and it is not in P0 to P3.

### 6.7 Kill switch

- **Scopes:** org, feature tag, model, account hash. The account hash is optional and supplied by the caller, so a call without it is outside an account-scoped switch: that scope stops cooperative callers only. The UI says so, and offers pairing it with a feature or org scope.
- **Triggers.** Manual (dashboard, API), and automatic *detection* of a spend runaway (for example spend in the last 10 minutes above N times the trailing hourly rate).
  - **Detection proposes; a person activates.** The kill switch has no shadow state, so an automatic trigger that acted on its first firing would bypass D10. It would also act on ai-tally's recorded spend, which this repo has seen inflate falsely: duplicated spans double-counted in rollups (#325), priced-$0 rows repaired after the fact (#327). A runaway detection therefore alerts and appears as a one-click proposal in the dashboard, with the evidence behind it.
  - **Automatic activation is opt-in per budget**, for orgs that prefer a false stop to a real overrun. An automatic activation **expires after 30 minutes** unless a person confirms it, and it is not sticky (below).
- **Delivery:** outbound only, because customer networks block inbound. The enforcement point holds a long-lived streaming connection (`GET /v1/governance/kill/stream`, server-sent events) and falls back to polling `GET /v1/governance/kill` every 5 seconds. Target propagation: under 10 seconds.
- **Serverless:** a function cannot hold a streaming connection or run a 5-second poller. The SDK in serverless mode checks kill-switch state **inline**, cached in module scope for `kill_ttl_seconds` (default 5): the first invocation after the TTL does a conditional `GET /v1/governance/kill`, so warm invocations pay nothing and one invocation per TTL pays a round trip. Propagation there is therefore bounded by the TTL plus that request, and the dashboard states it separately from the under-10-seconds target for long-running points. Customers who cannot accept a round trip on the request path can mirror the switch into their platform's own low-latency configuration store (Vercel Edge Config, AWS AppConfig with the Lambda extension); ai-tally writing to those stores is a P2 integration, not V1.
- **Sticky, when a person activated it.** A manually activated or confirmed switch stays active at an edge if the control plane becomes unreachable, until an explicit clear is received. This is the one exception to D5. An unconfirmed automatic activation is not sticky: it expires on schedule even if the control plane is unreachable, so a false detection cannot become an outage that outlasts the control plane's.
- **Today's precedent:** revoking an API key already stops proxy traffic within one 45-second key-feed refresh. The kill switch is the fast, scoped, reversible version of that.

## 7. Deployment modes

| Mode | Prompt leaves customer network | Added network hop | Counters | Routing | Kill switch | Best for |
|---|---|---|---|---|---|---|
| Hosted proxy (#384) | Transits ai-tally, never stored | Customer to ai-tally region | Exact (single instance today) | Recommend only (D9) | Stream, under 10s | Evaluation, pilots |
| In-VPC gateway (few replicas) | No | Inside their network | Redis, or leases | Recommend only (D9) | Stream, under 10s | Enterprise default |
| Sidecar | No | Localhost | Redis, or leases | Recommend only (D9) | Stream, under 10s | Latency-sensitive, strict isolation |
| SDK, long-running process | No | None | Redis, or leases | Native | Stream, under 10s | Python services |
| SDK, serverless (Vercel, Lambda) | No | None | Redis required | Native | Inline, TTL-bounded (6.7) | Serverless apps |
| SDK, scale-to-zero container (Cloud Run, App Runner) | No | None | Redis, or ephemeral leases (6.1) | Native | Inline, TTL-bounded, as serverless: a throttled idle container cannot keep a stream open | Containerized services that scale to zero |

**What serverless changes.** Beyond the kill switch, three things behave differently in an ephemeral function, and the SDK has an explicit serverless mode for them rather than pretending they are long-running:

- **Bundles** are cached in module scope and revalidated inline on a TTL (section 4.3), not polled.
- **Counters** cannot be local, because instances do not share memory and die without settling, so spend enforcement in serverless requires Redis. Without it, `spend` is capped at shadow in serverless mode.
- **Spans and decision records are flushed together** before the function is frozen. The SDK sends spans today from a daemon worker thread with an `atexit` drain (`sdk/python/src/tally/transport.py`), and a frozen serverless instance runs neither, so flushing only decisions would leave decisions pointing at spans that never arrive. Serverless mode drains both in the platform's post-response hook: `waitUntil` on Vercel, which is available to Python functions; on Lambda, a synchronous flush at the end of the handler, with an extension as an option for customers who cannot spend the time in the handler. Anything still lost is recorded as a gap in coverage, never silently.

The hosted proxy's single-instance deployment is not suitable for enforce mode at scale; section 13 treats this as a risk, not a footnote.

## 8. Data and telemetry

### 8.1 Decision log (ClickHouse, new)

```sql
CREATE TABLE governance_decisions (
    TenantId        String,
    Timestamp       DateTime64(3),
    TraceId         String,
    SpanId          String,
    FeatureTag      LowCardinality(String),
    AccountIdHash   FixedString(64) DEFAULT '',
    Capability      LowCardinality(String),
    RuleId          LowCardinality(String),     -- which rule within the capability, e.g. the budget id
    Attempt         UInt8,                      -- 0 for the call, 1+ for each retry or escalation in a cascade
    Mode            LowCardinality(String),     -- off | shadow | enforce
    Verdict         LowCardinality(String),     -- allow | would_act | acted | error
    Action          LowCardinality(String),     -- block | downgrade | route | redact | ...
    ReasonCode      LowCardinality(String),     -- budget_exceeded | pii_detected | model_denied | ...
    BundleVersion   UInt64,
    Fidelity        LowCardinality(String),     -- sdk_template | proxy_fingerprint | n/a
    Degraded        Array(LowCardinality(String)), -- bundle_stale | lease_unavailable | parse_rejected | request_too_large | sigpack_stale | ...
    RequestedModel  LowCardinality(String),
    RoutedModel     LowCardinality(String),
    ReservedMicroUsd Nullable(Int64),
    SettledMicroUsd  Nullable(Int64),
    TemplateHash    FixedString(64) DEFAULT '',
    EnforcementPoint LowCardinality(String)     -- hosted_proxy | vpc_proxy | sidecar | sdk
)
ENGINE = ReplacingMergeTree
ORDER BY (TenantId, Capability, Timestamp, TraceId, SpanId, RuleId, Attempt);
```

- No prompts, completions, variables or matched PII values. `ReasonCode` names the category; it never carries the matched text.
- `ReplacingMergeTree` on span identity **plus rule and attempt**, so replayed batches dedupe while distinct decisions on the same span survive. One call can produce several decisions for one capability (an org budget and a feature budget under `spend`; a retry and an escalation in a cascade); an earlier sorting key without `RuleId` and `Attempt` merged them into one row, and the shadow summary behind the enforce confirmation undercounted would-have-acted decisions. Every read uses `FINAL` (the guard from #381 must list this table).
- **Allow verdicts are counted, not logged row by row.** With up to six capabilities evaluated per call, one row per capability per call makes `allow` nearly all of the table. So rows are written for `would_act`, `acted`, `error` and any degraded decision, plus a 1% sample of `allow` decisions for audit, and every decision (allow included) increments a per-minute counter:

```sql
CREATE TABLE governance_decision_counts (
    TenantId     String,
    Minute       DateTime,
    Capability   LowCardinality(String),
    FeatureTag   LowCardinality(String),
    Mode         LowCardinality(String),
    Verdict      LowCardinality(String),
    Decisions    UInt64
)
ENGINE = SummingMergeTree(Decisions)
ORDER BY (TenantId, Minute, Capability, FeatureTag, Mode, Verdict);
```

  Coverage figures ("governed calls") come from the counts table; evidence and audit come from the rows. The dashboard never extrapolates the 1% sample into a count.
- `SettledMicroUsd` is null until settlement; unknown is null, never 0.

### 8.2 Span attributes

Added to `tally.schema` alongside the existing `gen_ai.guardrail.{rule_id}.*` attributes:

- `gen_ai.governance.bundle_version`
- `gen_ai.governance.verdict` (the most severe verdict on the call)
- `gen_ai.governance.routed_model` (when routing changed the model)

## 9. Control-plane API

All tenant endpoints use the service token plus `x-tenant-id`, like every `/v1/tenant/*` route. Edge endpoints authenticate with the enforcement point's own identity and name the tenant explicitly (section 9.1).

| Method | Path | Caller | Purpose |
|---|---|---|---|
| GET/POST | `/v1/tenant/governance/settings` | web | Master switch, staleness |
| GET/PUT | `/v1/tenant/governance/capabilities/{capability}` | web | Mode and params, optional `?feature_tag=` |
| GET | `/v1/tenant/governance/changes` | web | Audit log |
| POST/DELETE | `/v1/tenant/governance/kill` | web | Activate or clear a kill switch |
| GET | `/v1/tenant/governance/shadow-summary` | web | Would-have-acted counts and examples per capability |
| GET | `/v1/governance/bundle` | edge | Signed settings bundle, `ETag` by version |
| GET | `/v1/governance/routing/{sha256}` and `/v1/governance/sigpack/{sha256}` | edge | Content-addressed routing tables and signature pack, immutable, cacheable forever |
| POST | `/v1/governance/leases` | edge | Request, renew or return a spend lease (section 6.1) |
| GET | `/v1/governance/kill` and `/kill/stream` | edge | Kill switch state |
| GET | `/v1/governance/keys` | edge | Signed set of current bundle-signing public keys, verified against the shipped root key (4.2) |
| POST | `/v1/governance/decisions` | edge | Decision batches, same transport and idempotency as `/v1/batches` |
| POST | `/v1/governance/judge-results` | judge worker | Scores, verdicts, judge accuracy |

### 9.1 Edge identities

An earlier draft had every edge endpoint use "the tenant's API key". That fits only the SDK. A shared proxy serves many orgs and never holds their keys: the key feed ships only SHA-256 hashes (`infra/edge-proxy/internal/edgekeys/edgekeys.go`), and the proxy authenticates to the gateway with a service token. As written, a shared proxy could not fetch a bundle, request a lease or post decisions. So each enforcement point has its own identity:

| Enforcement point | Credential | May act for |
|---|---|---|
| Hosted proxy (run by ai-tally) | The gateway service token, as the key feed uses today | Any tenant, named per request |
| Customer-run proxy (in-VPC gateway, sidecar) | A **deployment credential**, minted per customer deployment and held by reference | Only the orgs listed on the credential |
| SDK | The org's API key, `write` or `admin` scope | Its own org only |

- Every edge endpoint takes the tenant explicitly (`x-tenant-id`), and the gateway checks it against the credential's scope. A credential for one org can never read another org's bundle, lease its budget, or write its decisions.
- A deployment credential is a new control-plane object: minted and revoked by an org admin, stored as a hash like an API key, and delivered to running proxies revoked-first through the existing key feed pattern.

## 10. Dashboard

- **Settings > Governance:** the master switch; each capability with its mode and params; per-feature overrides from P2. Admin-only controls, read-only for members. Not optimistic, like the #385 switch: a new state shows only once stored.
- **Enforce confirmation:** moving a capability to enforce shows its last 7 days of shadow decisions: how many calls it would have acted on, on which features and accounts, and example reason codes. If shadow has fewer than 100 decisions for that capability, the dialog says the sample is too small to judge.
- **Governance overview:** decisions over time by capability and verdict, top reason codes, spend saved by downgrades and routing (settled, not estimated), degraded-state banners (`bundle_stale`, `lease_unavailable`, `counters_unavailable`) where they occurred, and which budgets use `on_unavailable = shadow`.
- **Audit:** every settings change and kill switch event, with actor and bundle version.
- **Kill switch:** a prominent control with scope selection, and an always-visible banner while any switch is active.

## 11. Invariants respected

- **Honest under uncertainty.** Unknown settings resolve to shadow, not enforce. Unsettled spend is null. A quality score carries the judge's measured accuracy or says it is unknown. An inconclusive routing comparison is shown as inconclusive.
- **No bodies in telemetry.** Decisions carry categories, hashes and counts. Prompt checks run where the prompt already is. The hosted judge tier is a separate, consented carve-out and is out of P0 to P3.
- **Identifiers by hash, credentials by reference.** Account ids stay HMAC hashes; the bundle signing key is a KMS reference; template approval uses hashes.
- **Money is integer micro-USD.** Reservations, settlements, budgets and savings are integers; rate math uses `Decimal` and BigInt.
- **Control-plane writes through the gateway.** The web app changes settings through gateway endpoints only.
- **Edge proxy invariants.** Unchanged. V1 never rewrites a request body (D9). Request inspection is opt-in per capability, bounded, strictly parsed, and forwards exactly the bytes it inspected (D11).

## 12. Prerequisites

| Prerequisite | Needed before | Status |
|---|---|---|
| Real-traffic verification (#350) | `spend` in enforce | Harness shipped (#383). GitHub auto-closed #350 when #383 merged, but the run with real keys, checked against provider dashboards, has not happened |
| Per-org switch pattern (#385) | Section 3 implementation | Merged |
| Edge deployment credentials (9.1) | Customer-run proxies in P0 | Not built |
| API keys scoped to a feature tag (3.4) | Loosening overrides in P2 | Not built; `api_keys` has no feature column |
| Outcome signal per run (routing-intelligence P1) | `output_policy` metrics, routing P3 | Not built |
| Real replay corpus (routing-intelligence P0) | Routing tables | Not built; replay runs on mocks today |
| Subscription model (roadmap) | Plan layer (3.1, layer 2) | Not built; P0 treats every plan as included |
| Redundant hosted proxy | Enforce mode on the hosted proxy beyond pilots | Single droplet today |

## 13. Risks

- **ai-tally becomes critical infrastructure.** In enforce mode a governance bug can block production traffic. Mitigations: shadow-first (D10), fail toward shadow (D5), evaluation errors proceed (section 5.3), and a conformance suite across Go and Python.
- **The hosted proxy is one droplet.** Enforce on the hosted proxy beyond pilots needs redundancy first.
- **False positives erode trust quickly.** A PII or injection scan that blocks legitimate calls gets governance turned off. Mitigation: the enforce confirmation (section 10) and per-capability shadow data before enforcing.
- **Judge accuracy bounds every quality claim.** A 90% accurate judge cannot support a routing claim more precise than its error; hiding that would overstate the product.
- **Competitive overlap.** AI gateways already do routing and guardrails. The spec only pays off if routing decisions are visibly grounded in measured cost and quality per customer and feature.
- **Request inspection cost scales with prompt size.** D11 keeps it opt-in and capped, but long-context tenants who enable prompt policy in the proxy will see latency grow with request size. The SDK is the better enforcement point for them.
- **Parser differentials.** Any gap between the proxy's parser and a provider's is a policy bypass. Strict parsing (D11) closes the known class; a conformance test per provider, fed deliberately ambiguous bodies, keeps it closed.
- **Regex denial of service.** Pattern packs run on attacker-influenced input. RE2-only patterns (section 4.2) are the mitigation, and it adds `google-re2`, a compiled dependency, to the Python SDK.
- **Caller-set dimensions.** Feature tag and account hash come from the caller. Section 3.4 makes overrides tighten-only unless the tag is bound to a key, and section 6.7 states that account-scoped kill switches stop cooperative callers only. Any future policy that loosens on a caller-set value needs the same server-side binding.
- **SDK enforcement is cooperative.** The SDK runs inside the customer's process, so code that skips it, or calls a provider directly, is not governed. Only a proxy is non-bypassable, and only when the customer's network blocks direct egress to the providers. The spec's guarantees against a misbehaving caller (3.4, 6.7) hold fully at a proxy with egress controls and are best-effort in the SDK; the dashboard shows which enforcement point governed each call so the difference is visible.
- **Estimated mid-stream counts.** `truncate` for OpenAI and Anthropic depends on a calibrated estimate (5.2). It is conservative by construction and unavailable until calibrated, but a model change that shifts the output ratio widens `ε` until the calibration catches up.
- **Serverless is a weaker enforcement point.** TTL-bounded kill switch propagation, Redis-only spend enforcement, and decision loss on freeze without a flush hook. The dashboard must say which calls were governed from serverless.

## 14. Open questions

Resolved in review:

- ~~Policy language~~ **Decided: a custom JSON schema.** OPA/Rego brings a runtime dependency and evaluation latency that do not fit a 2 ms budget, and its expressiveness is not needed for the capabilities in section 4.1.
- ~~Shared counter store~~ **Decided: Redis, hardcoded for V1.** A pluggable store abstraction multiplies the test matrix for no V1 customer. Leases (section 6.1) cover deployments without Redis.
- ~~Proxy body rewriting (D9)~~ **Decided: not in V1.** Proxy routing is recommend-only; revisit no earlier than P4, under the constraints in D9.
- ~~Routing table size~~ **Decided: content-addressed per task type** (section 4.2).
- ~~What gets signed~~ **Decided: a manifest over the exact served bytes**, with a shipped root key and a fetched signing key set (section 4.2).
- ~~Automatic kill switch activation~~ **Decided: detection proposes, a person activates**; automatic activation is opt-in per budget and expires unconfirmed after 30 minutes (section 6.7).

Still open:

1. **`spend.on_unavailable` default.** `shadow` favors availability, `block` favors budget. The draft defaults to `shadow`; a finance-led buyer may expect `block`.
2. **Lease sizing defaults** (`lease_fraction`, `lease_max`, `min_lease`, expiry), which trade control-plane request volume against worst-case overrun.
3. **Redis from serverless:** accept connection setup on cold starts, or also support an HTTP-accessible Redis endpoint, which would reopen the "one store" decision.
4. **`google-re2` in the Python SDK:** acceptable as a hard dependency, or an optional extra that caps `prompt_policy` at shadow when absent?
5. **Signing key custody and rotation cadence** for both the root and the signing keys, and whether customers can pin their own verification key.
6. **Plan packaging:** which capabilities are in which plan tier, and whether shadow mode is free on every plan as the adoption path.
7. **Labeled sets for judge accuracy:** who produces them per task type, and the minimum size before a score is shown as measured.
8. **Hosted judge tier:** worth offering at all, given its consent and data-residency cost?
9. **Feature-scoped API keys:** a feature column on `api_keys`, or a separate binding table, and whether one key may be bound to several features.
10. **The allow sample rate** (1% in the draft) and how long the full decision rows are retained relative to the counts table.
11. **Ephemeral platform detection:** the environment signals per platform (Cloud Run is `K_SERVICE`; App Runner and Azure Container Apps need confirming), and whether detection defaults to ephemeral mode or only recommends it.
12. **Streaming calibration thresholds:** the 200-stream minimum and the p99 `ε`, and whether calibration pools across tenants for the same model to become available sooner.
13. **Signature pack corpora:** who owns the true-positive and false-positive corpora, and the false-positive rate a pack must stay under to publish.

## 15. Phasing

### P0: settings, shadow, decision log

Scope: section 3 settings and audit, with a missing capability row reading as off; resolution that checks the org switch before any degraded branch; edge identities (9.1), including deployment credentials; signed manifests, the root key and the signing key set; bundle compile and distribute; the decision log with rule and attempt in its key, the allow counts table and allow sampling; mode resolution in the SDK and proxy, with the effective-mode precedence for existing guardrail rules (3.2); the existing guardrail rule kinds and budgets evaluated in **shadow only**; the Governance settings page and shadow overview.

Done when: an org admin turns governance on, sees a week of would-have-acted decisions per capability with reason codes and bundle versions, turns it off, and traffic behaves exactly as before throughout.

### P1: deterministic enforcement and the kill switch

Scope: `spend` (reservation, settlement, once-per-period seeding and one-way reconcile, Redis counters, leases with the minimum lease, the 50 ms synchronous cap, `budget_leased_out` and ephemeral lease mode for scale-to-zero containers, `on_unavailable`, the streaming `truncate` action with a terminal error event, per-model output calibration and the conservative trigger), `model_policy` with `block`, D11 request inspection in the proxy with strict parsing and the hosted-proxy inspection confirmation, prompt scans (PII, secrets) with `block` on an RE2-only signature pack with post-match filters and the corpus publish gate, output schema `flag`, the kill switch with streaming delivery, the serverless inline check, and runaway detection as proposals with opt-in expiring auto-activation; the SDK's serverless mode (TTL bundles, spans and decisions flushed together); the enforce confirmation dialog. Gated on the #350 real-key run for `spend`.

Done when: a tenant enforces a monthly budget with downgrade, a disallowed model is blocked, a kill switch stops a feature's traffic within 10 seconds, and a control-plane outage during all of this causes no additional blocked calls.

### P2: prompt governance and per-feature scoping

Scope: `tally.prompt` template API, approved template versions, proxy fingerprinting with fidelity labels, scans on retrieved context and tool outputs, SDK `redact`, injection signals, per-feature-tag overrides that tighten only, and feature-scoped API keys for the overrides that loosen, with each key's bound tag carried in the key feed and the tag-resolution conformance fixtures in Go and Python.

Done when: a team enforces approved templates on one feature while the rest of the org stays in shadow, and the dashboard shows which calls had full template checking.

### P3: routing recommendations

Scope: routing tables from real replay and evaluation (prerequisites in section 12), the in-network judge worker with measured accuracy, recommendations with inconclusive handling, human approval of tables.

Done when: a recommendation shows a measured saving at a quality difference outside the judge's error, and an approved table routes a feature in the SDK.

### P4: automatic routing and cascade

Scope: automatic routing within approved bounds and cascade with bounded escalation, both in the SDK. Proxy routing is re-evaluated here under D9's constraints, as a separate decision, not assumed.

Done when: a feature runs on the cheapest model that clears its quality floor, escalates on post-check failure, and the dashboard reports settled savings net of escalations.

## 16. File-level change list (planned)

- **db/postgres:** `tenant_governance_settings`, `tenant_governance_capabilities`, `tenant_governance_changes`, governance bundle versions and signing metadata, `governance_edge_credentials` (9.1), per-model stream calibration, the spend lease ledger (6.1), and feature scoping for API keys (P2); mounts in `infra/docker-compose.yml`. Existing deployments pick these up through `make pg-migrate`, which `deploy.sh` now runs (#384).
- **db/clickhouse:** `governance_decisions.sql` and `governance_decision_counts.sql`; add both ReplacingMergeTree tables to the `FINAL` guard in `web/lib/clickhouse.test.ts` (the counts table is a SummingMergeTree and is read with `sum()`).
- **infra/gateway:** `governance_settings.py`, `governance_bundle.py` (compile, sign, content-addressed routing tables), `governance_sigpack.py` (publish gate: RE2 compile check, true-positive and false-positive corpora, recorded false-positive rate), `governance_leases.py` (ledger, minimum lease, `budget_leased_out`, ephemeral leases with heartbeat reclaim), `governance_seed.py` (settled-watermark seeding, one-way reconcile), `governance_signing.py` (manifests, root and signing key set; the gateway has no signing facility today), `governance_edge_identity.py` (9.1), `governance_decisions.py` (ingest, allow counts), `governance_kill.py` (manual switch, runaway proposals, expiring auto-activation); routes in `app.py`; tests including bundle signature, lease invariant (outstanding leases never exceed remaining budget), and resolution-order conformance fixtures.
- **sdk/python:** `tally/governance/` (bundle client and verification, mode resolution, pipeline, Redis counters and leases, RE2 scans via `google-re2`, `tally.prompt`, serverless mode with TTL revalidation and spans and decisions flushed together; the `BudgetTruncated` exception; ephemeral runtime detection with inline lease return; stream output estimation and calibration; tag resolution from key binding; post-match secret filters; effective-mode precedence with `GuardrailConfig.mode`); decision batching reusing the ingest transport; span attributes in `tally/schema.py`.
- **infra/edge-proxy:** `internal/governance/` (bundle client, resolution, D11 request inspection with a strict duplicate-rejecting JSON parser, pre and post checks, Redis counters and leases, stream output estimation with per-model calibration, tag resolution from the key's bound feature, post-match secret filters, kill switch stream); a per-provider parser-differential conformance suite. No request-body rewriting (D9).
- **judge worker:** a new container under `infra/judge-worker/`.
- **web:** `app/settings/governance/` (capabilities off until enabled, body-reading capabilities labeled, loosening overrides labeled, hosted-proxy inspection confirmation), `app/governance/` (overview from the counts table, audit, runaway proposals), `lib/governance.ts`, server routes enforcing `org:admin`; update the #385 proxy switch copy to mention inspection when a body-reading capability is on.
- **docs:** conformance fixture format; operator guide for the in-VPC gateway and shared counter store.

## 17. Success measures

- Governance adoption: share of active orgs with governance on in shadow, and share of those with at least one capability in enforce within 30 days.
- Inline overhead: p99 added latency per enforcement point, against section 5.1.
- Safety: zero calls blocked because of a control-plane outage or a governance evaluation error.
- Precision: shadow would-have-acted decisions later confirmed as correct by the admin before enforcing, per capability.
- Value: settled spend saved by downgrade and routing per month, net of escalations.

## 18. Revision log

**Review 1** (architecture review of the first draft):

- **D9 reversed.** Proxy model rewriting was not a scoped exception: it changes `Content-Length`, requires buffering and parsing, and opens a duplicate-key smuggling path. V1 proxy routing is recommend-only; the SDK routes.
- **D11 added.** The same analysis applies to *reading* a request, which the proxy does not do today, so request inspection is now opt-in, capped at 1 MiB and strictly parsed.
- **Budget overrun fixed (6.1).** The 60-second sync allowed replicas to collectively overspend. Replaced by spend leases bounded by remaining budget, with an explicit `on_unavailable` trade-off. Static per-replica partitioning was considered and rejected because it breaks under autoscaling and uneven load.
- **Serverless made explicit (4.3, 6.7, 7).** Inline TTL-bounded kill switch and bundle revalidation, Redis-only spend enforcement, and flushing decisions before the function freezes.
- **Bundle split (4.2).** Settings bundle, content-addressed routing tables and a separately versioned signature pack; no delta protocol in V1. Patterns must be RE2-compatible because the Python SDK's standard `re` is backtracking.
- **Open questions resolved:** custom JSON policy schema; Redis hardcoded for V1.

**Review 2** (second design review of the revised draft):

- **Settings resolution (3.3, 3.5, D5, D10).** A missing capability row now reads as off, so turning governance on no longer silently starts request inspection. The org switch is checked before degraded branches, so an unreadable setting never evaluates an org that opted out; a never-loaded bundle means off.
- **Edge identities (9.1).** Replaces "edge endpoints use the tenant's API key", which shared proxies cannot do: service token for the hosted proxy, scoped deployment credentials for customer-run proxies, API keys for the SDK.
- **Counters (6.1).** Seeded once per period from a settled watermark, moved only by reserve and settle, reconciled upward only; a new bundle version never reseeds. Leases have a minimum size (one worst-case reservation) and a 50 ms synchronous cap.
- **Overrides tighten only (3.4)** unless the feature tag is bound to a key, because the tag is caller-set.
- **Kill switch (6.7).** Runaway detection proposes and a person activates; automatic activation is opt-in and expires unconfirmed; only person-activated switches are sticky.
- **Decision log (8.1).** `RuleId` and `Attempt` in the sorting key so distinct decisions on one span survive; allow verdicts go to a per-minute counts table with a 1% row sample.
- **Guardrail mode precedence (3.2)** across capability mode, rule state and SDK mode.
- **Signing (4.2).** A signed manifest over the exact served bytes, a shipped root key and a fetched signing key set; avoids canonicalization and the KMS 4,096-byte raw-message limit.
- **Streaming (5.2).** Mid-stream stops only under an explicit `truncate` action, with a terminal error event and a typed SDK exception.
- **Hosted proxy inspection (goal 6, 3.6)** disclosed and confirmed separately; the #385 switch copy updated when it applies.
- **Serverless (7).** Spans and decisions flushed together, since the SDK's existing transport relies on a thread and `atexit` a frozen function never runs.
- **Latency (goal 3, 5.1).** End-to-end p99 per enforcement point and capability mix; per-step figures are targets, not a sum; network round trips reported separately.
- **Consistency and status.** Removed a failure-mode row that contradicted `on_unavailable`; #384 and #385 marked merged; #350's auto-close noted; account-scoped kill switches and proxy-only judge coverage stated.

**Review 3** (operational review of the Review 2 draft):

- **Lease hoarding on scale-to-zero containers (6.1, 7).** Containers on Cloud Run, App Runner and similar platforms look long-running but go idle holding leases. Added ephemeral lease mode: leases sized to in-flight need, the unused portion returned inline before the response is written (a post-response return is unreliable where CPU is throttled after the response), 10-second expiry, and heartbeat reclaim. Added `budget_leased_out`, so budget held in other leases never presents as a spent budget.
- **Mid-stream output counting (5.2).** Truncation only applies to requests without `max_tokens`. Gemini gives exact running counts; OpenAI and Anthropic are estimated from emitted content with a per-model ratio whose p99 error is measured from each stream's final usage. The trigger cuts only when the low end of the estimate crosses the limit, and `truncate` is unavailable until a model is calibrated.
- **RE2 without lookarounds (4.2).** Matching and false-positive filtering are separate stages; filters (entropy, shape allowlists, keyword proximity, checksums) run in code on matches only. Rules from RE2-native scanners such as gitleaks are adapted after license and corpus checks; rules from backtracking engines are not imported as-is. Added a publish gate with true- and false-positive corpora.
- **Feature tag resolution (3.4).** Exact rules: a feature-scoped key's bound tag governs; with an org key the header tag never reaches a loosening override; resolution happens at the enforcement point from the key feed, pinned by conformance fixtures in Go and Python. Added the risk that SDK enforcement is cooperative and only a proxy with egress controls is non-bypassable.
