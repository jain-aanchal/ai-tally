# Scope: routing intelligence

**Status: proposal.** A response to the "making nondeterministic calls behave deterministically"
thesis, tested against what this repository actually contains rather than against what the layers
sound like they need.

## What this is and is not

This doc answers three questions about the thesis: what already exists, where the thesis collides
with something real in this codebase, and what the smallest first step is. It does not restate the
thesis. It is not a plan of record and it does not commit anyone to building routing.

It is not `docs/specs/initiative-agent-to-agent.md`, which scopes the surface an agent uses to bring
a savings proposal to its human. That doc consumes findings; this one is about whether a new class of
finding is measurable at all. Where they touch, see the section on how they relate.

The short version. Layers 1, 6 and 7 are mostly a matter of connecting pieces that already ship.
Layer 2 is a real gap the thesis correctly diagnoses. Layers 3, 4 and 5 do not exist in any form and
two of them collide with the no-bodies invariant, though the collision is narrower and better
precedented than CLAUDE.md's one-line statement of it suggests. The headline cascade arithmetic is
correct as printed and the conclusion it supports is not, because the verifier's own error rate is
absent from the model and dominates the result.

## What exists today, verified

Read from files on this branch, not inferred. All paths are repository-relative.

### The replay and eval machinery is complete, shipped, and fed by mocks

`infra/gateway/src/gateway/app.py` exposes `POST /v1/replay` (line 3190), `POST /v1/replay/estimate`
(3387) and `POST /v1/eval` (3663), plus the per-tenant config endpoints under `/v1/tenant/replay/*`
and `/v1/tenant/eval/*`. Replay picks a stratified sample from a captured corpus, calls a candidate
model per sample, prices the returned tokens through the catalog
(`replay_executor.ReplayExecutor.replay_sample`, line 131, pricing at 190), and reports projected
monthly cost, p50 and p95 latency and an error rate. Eval takes those runs and puts them through a
pairwise judge with a position-bias coin flip (`eval_executor.EvalExecutor.judge_pair`, line 181,
randomisation at 221) under `RUBRIC_VERSION = "rubric-v1"`, and reports a win rate with a Wilson
interval computed by `app._wilson_interval` (line 3936).

Both paths are budget-capped per tenant, concurrency-limited
(`MAX_CONCURRENT_PER_TENANT = 5` for replay, `3` for eval), memoised for fifteen minutes by
`replay_perf.ProjectionCache` (`PROJECTION_CACHE_TTL_S = 15 * 60`), and backed by an LRU of parsed
envelopes (`replay_perf.EnvelopeCache`, `ENVELOPE_CACHE_MAX = 4096`) so the judge loop does no
blocking blob reads. `replay_perf.ReplayRunStore` caps stored runs at
`REPLAY_RUNS_PER_TENANT_CAP = 2000` and upserts on
`(tenant_id, sample_id, candidate_provider, candidate_model)` so a repeated projection replaces
rather than stacks.

The corpus itself is `replay_samples` (`db/clickhouse/replay_samples.sql`), written inline on every
accepted `/v1/batches` call via `capture_replay_samples_for_batch` (app.py 3134), wrapped so a
capture failure never fails an accepted ingest. Capture is per-tenant opt-in and off by default
(`tenant_replay.DEFAULT_CONFIG`: `enabled=False`, `sample_rate=0.05`, `retention_days=30`,
`daily_budget_usd=Decimal("5.00")`). Sampling is stratified by `(feature_tag, token-count quintile)`
with the top quintile drawn at twice the rate, so cost outliers survive
(`replay_sampler.stratified_sample`, line 136). Every payload is PII-scrubbed before it reaches
object storage: emails using the ingest validator's own regex, a narrow list of provider API-key
prefixes, and a deliberately conservative postal-address heuristic (`replay_sampler.scrub_pii`).

**Both the candidate client and the judge default to deterministic mocks and nothing in the
repository wires a real one.** `app.py` line 3272 and 3462 fall back to `_mock_candidate_client`,
which echoes the envelope's own token counts back. Line 3772 falls back to `_mock_judge_client`,
which hashes the prompt with blake2b modulo 40 into roughly 47.5 percent A, 47.5 percent B and
5 percent tie, deliberately balanced so a win rate lands near 0.5 with an interval straddling it.
Grepping for `replay_candidate_client` and `eval_judge_client` outside `app.py` finds only test
fixtures. So the pipe is built end to end and no real measurement has ever flowed through it.

### `wrong-sized-model.ts` is Layer 1 and the confidence bullet, already shipped in a narrow form

`web/lib/waste/wrong-sized-model.ts` takes an incumbent model resolved per feature from real spend,
its measured per-call cost, and a set of candidates each carrying a replayed per-call cost and a
judged win rate with its interval. Its constants are worth quoting because they are the sample-size
discipline the thesis's last build-list bullet asks for:

```ts
const PAIRWISE_EVEN = 0.5;
const MIN_JUDGED_SAMPLES = 10;
const MIN_REPLAYED_SAMPLES = 50;
const TIGHT_CI_WIDTH = 0.1;
const MAX_FEATURES = 12;
```

A candidate qualifies only if it is a genuinely different base model (suffix-stripped by
`baseModelId`), clears both sample floors, is cheaper per call than the incumbent, and has a
confidence interval whose upper bound still reaches the pairwise even line. A candidate whose whole
interval sits below 0.5 is rejected even when it is cheaper. Confidence is `high` when the interval
width is at most 0.1 and `medium` otherwise, never `low`, and it gates only the label, never the
dollar figure. When no candidate qualifies the detector emits nothing rather than a zero-dollar
finding, and there is no mock or static fallback path anywhere in it.

That is Layer 1 for one narrow question ("is this feature on a model that is bigger than it needs to
be") and it is the sixth build-list bullet ("confidence published alongside every recommendation")
already in production. What it is missing is not statistics. It is a real judge, a real candidate
call, and a corpus with something in it to judge.

### `replay_samples` holds no bodies, and the corpus is empty of content

This is the single most consequential finding in the investigation.

`replay_samples` stores `TenantId`, `SampleId`, `TraceId`, `FeatureTag`, `RealProvider`, `RealModel`,
`InputTokens`, `OutputTokens`, `CapturedAt`, `S3ObjectKey`, `PIIScrubbed` and `ContextFidelity`. No
body and no content hash. The body is supposed to live in object storage at
`tenants/{tenant_id}/replay_samples/{yyyy/mm/dd}/{sample_id}.json`
(`replay_store.build_replay_object_key`), behind `InMemoryReplayBlobStore`, `GCSReplayBlobStore` or
`S3ReplayBlobStore`.

`replay_sampler.SampleCandidate` documents its `envelope` field as "The full resolved request
envelope: prompt, tools, model config, response." The only production caller populates it with this
(app.py, line 911):

```python
envelope={
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
    "real_provider": str(provider) if provider else "",
    "real_model": str(model) if model else "",
    "feature_tag": feature_tag if isinstance(feature_tag, str) else "untagged",
    "context_fidelity": "resolved-context",
},
```

Counts and labels. No prompt, no tools, no response. The blobs the corpus points at are token-count
JSON, which is why the mock candidate client can echo them back and why the judge's
`_extract_instruction` and `_extract_response` helpers (app.py 3893 onward) are resilient "to several
shapes": there is nothing to extract. The local stack's reported 903,081 rows at 53.82 MiB works out
to roughly 62 bytes per row, which is consistent with an index-only table and inconsistent with
anything carrying content. I could not verify the row count directly, since the local ClickHouse
refused the default credential during this investigation, so treat the figure as reported rather than
confirmed. The shape of it corroborates the code either way.

So the substrate for a frozen eval set exists as a schema, a sampler, a scrubber, a retention knob and
a store abstraction with three backends. It does not exist as data.

### p(success) is not derivable today, and for SDK tenants it is structurally unavailable

`otel_spans.StatusCode` is a plain `UInt8` carrying OTel semconv values, 0 unset, 1 ok, 2 error. It is
set in exactly one place in the whole system: `infra/edge-proxy/internal/telemetry/telemetry.go` line
183, as `statusError` when the upstream was unreachable or the provider returned HTTP 400 or above.
The Python SDK never sets it. `SpanFields` in `sdk/python/src/tally/schema.py` has no status field at
all, and the gateway's `mapping.py` line 248 coerces an absent value to `0` through `_i`. Every span
an SDK-instrumented tenant emits therefore lands as Unset, and the entire read path treats Unset as
success. `web/lib/clickhouse.ts` line 2412 says so directly: only success and failed are inferable
from StatusCode, and abandoned is not tracked.

Two consequences. First, for an SDK-only tenant the observed failure rate is structurally zero, so
`paid-for-nothing.ts` and `duplicated-work.ts` can never fire for them, and Layer 1 has no signal to
measure. Second, even for edge-proxy tenants, StatusCode 2 means the HTTP call failed, not that the
task failed. A model that returns 200 with a wrong answer is indistinguishable from a correct one.

The thesis rests entirely on p(success) per task type. It is not measurable in this system today, by
either instrumentation path, for either meaning of the word.

### The taxonomy is a labeling convention, and Layer 2 is right about that

`FeatureTag` is a free-form `LowCardinality(String)`. In the SDK it is `GenAI.FEATURE_TAG` in
`_STR_KEYS`, and `validate_span_attributes` checks only that it is a non-empty string
(`sdk/python/src/tally/schema.py` lines 248 to 252). No allowlist, no enum, no format rule. The
gateway has an optional unknown-tag warning, `SpanValidator(known_feature_tags=...)` in
`validation.py` lines 104 to 148, and **no caller ever passes it**, so the non-fatal
`UNKNOWN_FEATURE_TAG` code is dead. Untagged spans become the literal string `"untagged"`
(`mapping.py` 249).

`GenAiOperation` is likewise open. `schema.py` line 84 defines
`OPERATIONS = frozenset({"chat", "completion", "embeddings", "tool", "agent", "rerank", "vector"})`
with the comment "open set; unknown values are allowed", and the validator never references it. The
only check is a lowercase warning. Compare `_SAMPLING_STRATA` in the same file, which is enforced,
with the comment explaining that the validator rejects anything else "so we don't end up with a
long-tail of free-text strata". Operations got the comment and not the enforcement.

Span naming is not a taxonomy either. The SDK never sets a span name; `mapping.py` line 247 assigns
`SpanName` from an explicit value, else the operation name, else the literal `"llm.call"`, which is
what every edge-proxy span gets. There is no `AgentName` column; agent identity is `ServiceName`. So
the classification axes in the sort key, `(TenantId, FeatureTag, ServiceName, SpanName, ...)`, are
three free-text fields the tenant chooses and one default.

The thesis's claim that the taxonomy is a product decision rather than a labeling convention is
correct, and the codebase currently sits on the labeling-convention side of that line by default and
by omission rather than by decision.

### The scheduler can carry "replayed on a schedule" with almost no work

`infra/gateway/src/gateway/scheduler.py` is an engine and registers nothing itself. It is a tick loop
(`tick_interval_s`, default 300 seconds) that asks per job and tenant whether a fixed interval has
elapsed since the last settled run, against run history in `scheduler_runs`
(`db/postgres/0027_scheduler_runs.sql`). It has per-tenant state, multi-replica safety via a Postgres
advisory lock, exponential backoff on repeated failure from a 300 second base capped at six hours, and
a `RunStatus` of success, skipped or failed. It is off by default (`settings.scheduler_enabled`).

Registered jobs, in `app.py` lines 456 to 470: `cost_connectors` daily, `stitcher` hourly,
`ingest.segment` and `ingest.hubspot` every fifteen minutes, `ingest.pendo` every thirty,
`reconciliation` hourly. Neither replay nor eval is among them, and there is no job enforcing
`retention_days` or purging blobs either. Registering a weekly replay-and-eval job is a handful of
lines against a mature engine. The hard part of Layer 7 is not scheduling.

### Layer 6 has a partial answer, and a cautionary tale attached to it

`web/lib/waste/duplicated-work.ts` groups whole traces by `feature + agent + model + userIdHash`,
chains them into bursts when each run is within `RETRY_WINDOW_SECONDS = 5 * 60` of its predecessor,
and flags a run as superseded only when it failed and a strictly later run in the same burst
succeeded. It states out loud in its own reason string that "same shape" is those four dimensions plus
time proximity and is not proof of identical inputs, because telemetry carries no prompt or completion
text.

The comments record why the rule is that narrow. The earlier rapid-repeat rule conflated normal
multi-turn conversation with waste and produced roughly 19,700 dollars of fabricated recoverable spend
on the demo corpus before review caught it (CTO-227). That is direct, in-repo evidence that "repeat
implies waste" overcounts badly, and it should temper Layer 6's expectations before the first customer
conversation, not after.

The detector's confidence is hard-coded `medium` and it is entirely gated on `max(StatusCode) = 2`,
which per the section above only the edge proxy ever produces.

### The edge proxy already holds the body and throws it away on purpose

`infra/edge-proxy/internal/proxy/proxy.go` wraps the upstream response in `metaCapture`
(`provider.go` line 143), which streams the body through untouched while teeing a bounded copy, capped
at 1 MiB, purely so `extractMeta` can pull scalar metadata: model, token usage, and for Gemini the
path-derived model. Pure pass-through routes never inspect a body at all. This matters enormously for
the collision section: the full request and response are already in the customer's own process, in the
customer's own network, and are deliberately reduced to scalars before anything leaves.

### The nearest thing to a verifier that exists

`sdk/python/src/tally/evals.py` has `CorrectnessEvaluator` (LLM-as-judge with an injected callable, no
network), `FormatAdherenceEvaluator` (JSON parse, regex, required keys) and `RefusalEvaluator`. These
score replayed outputs, not live traffic, and never write a per-span outcome. `FormatAdherenceEvaluator`
is a post-hoc format check rather than a schema constraint on generation, but it is a real verifier
skeleton with a real test suite, and it is the obvious place to grow Layer 4 rather than starting from
nothing.

Grepping the whole tree for `response_format`, `json_schema`, `tool_choice`, `schema_constrained`,
`verifier` and memoisation finds nothing else relevant. `CachedInputTokens` on a span is the provider's
own prompt cache as reported in usage, not ours. There is no response cache and no content-hash dedup
anywhere. `otel_spans.ResolvedPromptHash` is a `FixedString(64)` that exists and that no query groups
on.

## The no-bodies collision, precisely

CLAUDE.md states the invariant as "counts, hashes and mapped events only. No prompts, completions or
retrieved text reach storage." Read literally that forbids Layers 3, 4 and 6 outright. Read against
the code it is narrower than that, and the narrowing is deliberate, documented and consented.

**What the invariant actually binds.** It binds `otel_spans` and business events, and it is enforced
there mechanically. `mapping.py::_is_body_key` drops any attribute whose last dot-segment is one of
`message_text`, `messages`, `prompt`, `prompt_text`, `completion`, `completion_text`, `input_text`,
`output_text`, `content`, `text` or `body`. That is a real guard on the telemetry hot path and nothing
proposed here should weaken it.

**A body carve-out already exists.** `db/clickhouse/replay_samples.sql` adds
`replay_runs.ResponseText String DEFAULT '' CODEC(ZSTD(1))` under a comment headed "PII CARVE-OUT",
which says in as many words that this is the verbatim candidate-model response body, that replay is a
separate opt-in path with its own retention and access tier, and that persisting it "does NOT relax
the span-side no-bodies invariant". It is backed by an informed-consent string a tenant sees when
enabling replay (`tenant_replay.CANDIDATE_RESPONSE_RETENTION_CONSENT`), by
`replay_executor.replay_sample` line 211, and by explicit exclusion from both warehouse exports:
`bq_export.py` line 50 and `athena_export.py` line 32 both say the replay tables are their own opt-in
tier and are not exported.

So the honest statement of the current position is this. **ai-tally already stores model output bodies,
under an opt-in tier with its own consent, retention and export exclusion. It stores no request bodies
anywhere, and the invariant in CLAUDE.md is about telemetry, not about the whole system.** The doc
should be amended to say that, because right now a reader of CLAUDE.md would conclude something about
`replay_runs` that is not true, and a reviewer citing the invariant against a Layer 4 proposal would
be citing a rule that has a published exception.

Two further qualifications keep this from being a free pass. First, `replay_runs` is never actually
written to ClickHouse: `REPLAY_RUN_COLS` has no callers and runs live only in the in-process
`ReplayRunStore`. So the carve-out is authorised and not yet exercised in storage. Second, and more
important, the carve-out covers the **candidate's response**. It does not cover the customer's request,
their prompt, their retrieved context, or their production model's output. Layers 3, 4 and 6 all need
one or more of those. Extending the carve-out to cover them is a new consent decision with a much
larger privacy surface, not an application of an existing one.

### Where each layer sits

| Layer | What it needs | Relative to the invariant |
| --- | --- | --- |
| 1, measure variance | An outcome label per run. Not a body. | Compatible. Needs a verdict field, which is a mapped event. |
| 2, task granularity | A validated tag. Not a body. | Compatible. |
| 3, constrain output | The request's schema and tool config at the call site, plus a matched-pair measurement | Needs request bodies for the measurement, unless the comparison is done in-process. |
| 4, verifiers | The output, to check it | Needs the output. In-process this is free; server-side it is a new carve-out. |
| 5, cascade routing | The prompt, to re-send it to the next model | Needs the request. In-process this is free; server-side it is a new carve-out. |
| 6, memoize | The stored answer, to serve it | Cannot be done at all without retaining answers. Reporting repeat rate can be done from hashes alone. |
| 7, drift | A frozen corpus and a verdict per replay | Needs whatever Layer 4 needs, plus retention that outlives `retention_days`. |

### The four resolutions, assessed

**Do it in the edge proxy or the SDK.** The body is already in the customer's process. `metaCapture`
already tees a bounded copy. Running a deterministic verifier there, computing a request-shape hash
there, and emitting only a verdict and a hash costs no new data movement and changes nothing about
what leaves the process. This resolves Layers 1, 3, 4 and 6-as-reporting completely and cleanly, and
it resolves Layer 5 too, because the escalation to the next model also happens in the request path
where the prompt already is. The verdict is a mapped event and the hash is a hash, which is precisely
what the invariant permits.

The cost is that it moves work into a Go proxy and a Python SDK that both sit in the customer's latency
path, that verifiers become customer-authored code we execute, and that the SDK path needs a way to
receive a verdict it does not currently have anywhere in `SpanFields`.

**Store hashes only, body in the customer's store.** This is already designed and already unbuilt.
`sdk/python/src/tally/object_storage.py` defines `ObjectCategory.RESOLVED_CONTEXT` with a 30-day
retention default, a 64 KiB inline threshold, mandatory server-side encryption, and per-tenant plus
per-region key prefixing "so a bucket is never shared across isolation boundaries".
`otel_spans.ResolvedContextRef` is the pointer column. Neither is populated by any production path.
If that bucket is the customer's, replay gets real fidelity and ai-tally stores a pointer. This is the
right answer for Layer 5 measurement and for a self-hosted deployment it is nearly free, since the
gateway is the customer's too.

**A customer-hosted cache keyed by request-shape hash.** The clean answer for Layer 6. ai-tally reports
the repeat rate per task type from hashes, which requires no bodies and is a small delta on
`duplicated-work.ts`. The customer runs the cache, owns the staleness policy, and owns the correctness
risk. We should not want to own that risk; see the pushback section.

**Extend the CTO-125 carve-out to request bodies.** Smallest engineering change, largest privacy delta.
There is precedent, a consent mechanism, a retention knob and an export exclusion already in place. It
is the right option only for tenants who explicitly want ai-tally-side quality judging and are willing
to pay for it in trust. It should never be the default and it should never be the only path, because a
hosted product whose value proposition requires customer prompts on our disks is a different product
with a different sales cycle.

**Recommendation.** In-process verification and hashing as the default, customer-owned resolved-context
storage for replay fidelity, and the extended carve-out strictly as an opt-in for tenants who ask.
That combination keeps every layer except Layer 6's cache-serving inside the invariant as written, and
Layer 6's cache-serving is something the customer should own regardless of privacy.

## Where the thesis does not hold

### The arithmetic is correct. Check it, then look at what it leaves out.

Independent case, all four figures verified. Mid at 0.09 times 0.030 is 0.0027. The double-failure
fraction is 0.09 times 0.03, which is 0.0027, and frontier on it is 0.000486, printed as 0.0005.
Total 0.007186, printed as 0.0072. Combined success 1 minus 0.000027 is 0.999973, printed as 99.997
percent. Against 0.180 that is 25.05x, printed as roughly 25x. All correct.

Correlated case, also correct as arithmetic. 0.004 plus 0.0027 plus 0.00486 is 0.01156, printed as
0.0116. Combined success 1 minus 0.0108 is 0.9892, printed as 98.9 percent. Against 0.180 that is
15.5x, printed as around 15x.

One wording problem. The correlated case is described as "still around 15x cheaper at comparable
reliability", but 98.9 percent is **below** the frontier model's own 99 percent. The cascade in the realistic case produces 1.08 percent failures against the frontier's 1.0
percent, which is 8 percent more failures. That is comparable, and it is not the "more reliable"
claimed two paragraphs earlier for the optimistic case. The doc should say plainly that the realistic
cascade trades a small amount of reliability for a large amount of money, because that is a perfectly
good trade and overstating it is the thing that gets caught.

### Verifier accuracy is the hidden variable, and it dominates

Both combined-success figures assume the verifier is perfect. Let β be the probability the verifier
passes an output that is actually wrong, and α the probability it escalates an output that was
actually right.

For the correlated three-stage cascade, the fraction of tasks that ship a wrong answer is
0.09β plus 0.027β(1 minus β) plus 0.0108(1 minus β) squared. Working that through:

| β | Shipped wrong | Combined reliability |
| --- | --- | --- |
| 0 | 1.08% | 98.92% |
| 2% | 1.27% | 98.73% |
| 5% | 1.55% | 98.45% |
| 10% | 2.02% | 97.98% |
| 20% | 2.92% | 97.08% |

For the independent case the effect is far more violent, because the claim being made is so much
stronger. Shipped wrong is 0.09β plus 0.0027β(1 minus β) plus 0.000027(1 minus β) squared:

| β | Combined reliability | Claimed |
| --- | --- | --- |
| 0 | 99.997% | 99.997% |
| 0.1% | 99.988% | 99.997% |
| 1% | 99.90% | 99.997% |
| 5% | 99.53% | 99.997% |

The structural point matters more than any row. Shipped-wrong is at least 0.09β no matter how many
stages you add, because a wrong first-stage output the verifier misses never reaches stage two.
**Combined reliability is capped at roughly 1 minus 0.09β regardless of the model chain.** Adding
models cannot buy past the verifier. A 99.9 percent outcome SLA therefore requires β at or below 1.1
percent: the verifier must miss fewer than about one in ninety real failures. The advertised 99.997
percent requires β around 0.03 percent.

False positives hit cost rather than reliability. In the correlated case, α of the 91 percent that
were already right get escalated to the mid model, adding 0.0273α. At α equal to 5 percent that is
0.0014, which is 12 percent on top of the 0.0116 figure, and it compounds at the next stage.

**And the verifier's own error rate is itself unmeasured, which makes Layer 4's claim circular.**
Layer 4 says a verifier "produces a label without waiting for a human, which resolves the
accuracy-label question in any design partnership where the partner's human review layer is thin."
That is only true after β is known, and estimating β requires exactly the human ground truth the
verifier was supposed to replace. By the rule of three, bounding β at or below 1.1 percent with 95
percent confidence and zero observed misses needs roughly 273 labeled genuine failures. At a 9 percent
failure rate that is about 3,000 labeled tasks per verifier. The verifier does not eliminate the human
label. It amortizes it across all future traffic, once per verifier, and again whenever the task type
or the model shifts enough to invalidate the estimate. That is a good deal and it should be sold as
that rather than as elimination.

### Sampling cost is fine, and the thesis is measuring the wrong variance

Layer 1 says run the same task 20 times. For the product claim being made, that is the wrong
experiment. The customer-facing bound is "this task type completes correctly 99.9 percent of the time",
which is a statement about the **input distribution**, not about repeats of one input. Twenty runs of
one prompt measures within-input nondeterminism, which is a useful diagnostic for whether a task type
is well posed, and which is not what the SLA is about. Twenty distinct production inputs at one run
each costs the same and answers the actual question. The thesis conflates the two and pays 20x for the
less useful one.

Take the cost seriously either way. Refresh cost is task types times models times runs times mean cost
per call. Using the thesis's own three price points, mean 0.0713 dollars, purely as an illustration
since those figures are illustrative:

- 20 task types, 12 models, 20 runs is 4,800 calls, about 342 dollars per full refresh. Weekly for
  drift, about 17,800 dollars a year, per tenant, before the judge.
- The judge is the part that hurts. `tenant_eval` defaults `judge_model` to `claude-opus-4-8`, the most
  expensive tier available, and a pairwise judgment costs more than the small-model call it is grading.
- The 99.9 percent claim is what breaks it. By the rule of three, observing zero failures in n trials
  supports a failure-rate bound of 3/n, so a 99.9 percent floor needs about 3,000 verified observations
  per cell. Twenty task types by twelve models by 3,000 is 720,000 calls, roughly 51,000 dollars per
  refresh at the same mean, per tenant. Weekly, that is not a business.

The conclusion is not that Layer 1 is unaffordable. It is that **the cheap claim and the expensive
claim are different products**. Distinguishing 91 percent from 97 percent takes a few hundred samples
per cell and costs tens of dollars. Certifying 99.9 percent takes thousands per cell and costs
thousands of dollars per refresh. The thesis opens by choosing the 99.9 percent framing in its example
sentence, which is the expensive one, and closes with a build plan sized for the cheap one.

Worth noting against shipped config: `tenant_replay_config` defaults `daily_budget_usd` to 5.00 and
`tenant_eval_config` to 10.00. A 342 dollar refresh is 68 times the replay budget. Whatever the answer
is, those defaults and the budget-exclusion path in `replay_executor` need revisiting before anything
here runs on a schedule.

### Selection effects are worse than the thesis admits, and Layer 2 makes them worse still

Conditional success rates are observable only on cascades that actually ran. If routing sends easy
tasks to the small model, the observed p(mid succeeds given small failed) is conditioned on the routing
policy, not on task difficulty, and it moves every time the policy moves. The estimate is not merely
noisy, it is a moving target that the product's own recommendations perturb.

The standard fix is an exploration arm: route a fixed random fraction through the full cascade
regardless of policy, and estimate the conditionals on that arm only. It works, and it costs money on
purpose, in a product whose pitch is that it saves money. That tension should be priced and disclosed
rather than discovered by a customer reading their bill.

There is a second interaction the thesis does not mention. Layer 2 says the taxonomy should be
re-cut when decomposition reduces variance. Every conditional success rate is estimated **per task
type**. Re-cutting the taxonomy invalidates the accumulated conditionals for the old task types, and
the new finer types each need their own sample count. With this codebase's floors, decomposing one task
into eight steps means eight cells each needing `MIN_REPLAYED_SAMPLES = 50` and `MIN_JUDGED_SAMPLES =
10` before anything renders, and under the honest-under-uncertainty invariant the dashboard correctly
blanks all eight until they fill. Layers 2 and 5 are in direct tension and the plan needs to say which
one gives.

### "Nobody publishes p(success)" is nearly right, and overclaimed as stated

Public benchmark suites do publish pass rates, and some publish spread across runs. What is genuinely
unpublished is p(success) on a **customer's own task types**, and the cross-model conditional
structure. Those are the defensible parts and the claim is stronger when narrowed to them. As written
the sentence invites a reviewer to produce a counterexample in thirty seconds, which costs credibility
that the real claim does not need to spend.

### Layer 6's premise is the one most likely to embarrass a demo

"Same request shape, same answer, already approved by a human once" is doing a lot of work. Two
problems.

First, this codebase has already made this mistake and documented it. The original `duplicated-work.ts`
rule treated rapid repeats as waste and fabricated roughly 19,700 dollars of recoverable spend on the
demo corpus before CTO-227 narrowed it to error-then-retry. Any repeat-rate figure shipped without that
narrowing will overcount for the same reason: a multi-turn conversation is repetitive by nature and is
not waste.

Second, and worse, the thesis's own Layer 4 examples are exactly the tasks you must not cache. "Does
that unit ID exist in the database" has an answer that depends on mutable state. Serving that from
cache is not a saving, it is a correctness bug with a cost report attached. Cache validity is a
customer-domain judgment about which task types are pure functions of their input, and ai-tally is not
positioned to make it. Reporting repeat rate per task type is safe, cheap, needs no bodies, and is a
genuinely useful number. Serving from cache should belong to the customer.

### Layer 7's frozen set collides with replay retention, twice

`retention_days` defaults to 30, and freezing an eval set means keeping it beyond that. Worse, the
retention is documented and not implemented: both `GCSReplayBlobStore` and `S3ReplayBlobStore` state
that lifecycle "is expected to be enforced by a bucket lifecycle policy provisioned out-of-band" and
neither creates one, while `replay_samples` has no ClickHouse `TTL` clause at all (the only `TTL` in
`db/clickhouse/` is on `otel_spans`). So the index rows are immortal and the blobs are governed by a
policy nobody has written. When a bucket policy does land, index rows will outlive their blobs and
every affected sample will raise `ReplayBodyMissing` and be silently skipped, surfacing only as
`excluded_missing_body_count`. A frozen eval set needs its own retention tier and its own consent, not
an exemption bolted onto the sampling one.

There is a methodological problem too. A frozen set drifts away from the production distribution as the
customer's traffic changes, so a fall from 96 to 88 percent is model drift, traffic drift, or the set
going stale, and a frozen set cannot separate them. Refreshing the set removes the ability to attribute
the change at all. The usual answer is two sets, one frozen for attribution and one rolling for
relevance, which doubles the sampling cost calculated above.

### Two smaller things

The cascade's cost model assumes each stage re-runs the same task. In practice you either re-roll the
dice blind, which wastes the information the verifier just produced, or you feed the failing output and
the verifier's complaint to the next model, which lengthens its input and raises its cost above the
table's figure. Neither is wrong, and the doc should pick one.

The thesis correctly flags latency as excluded and should quantify the shape. In the correlated case,
9 percent of traffic pays small plus verify plus mid serially and 2.7 percent pays five stages. The
cascade converts a mean-cost problem into a p95-latency problem, and for anything user-facing that is
the constraint that actually decides adoption.

## What would have to be built

Ordered so that each phase produces something a customer can see, and so that the earliest phase
unblocks the most of what follows.

### P0: make the existing replay and eval path real

The whole Layer 1 apparatus ships today and measures nothing, because the corpus holds token counts and
the clients are mocks. This is the highest-leverage change in the document and it is not large.

Scope: populate `SampleCandidate.envelope` with the real resolved request and response for the opt-in
replay path only, sourced from the edge proxy where `metaCapture` already holds them, or from an
explicit SDK call, and stored under the customer-owned `RESOLVED_CONTEXT` scheme rather than in a
shared bucket. Wire a real `replay_candidate_client` and a real `eval_judge_client` behind config.
Decide the retention and consent tier for request bodies explicitly, since the CTO-125 carve-out does
not cover them.

Done when: `wrong-sized-model.ts` renders a finding whose win rate came from a real judge grading real
candidate output against real incumbent output, with its existing floors and interval unchanged, and a
tenant who has not opted in sees exactly what they see today.

### P1: an outcome signal

Nothing above Layer 1 is measurable without p(success), and StatusCode cannot carry it.

Scope: a per-run outcome distinct from transport status, emitted from the customer's process. Start
with the transport-honest version, which means giving the SDK a status field at all so an SDK tenant
stops reporting a structurally impossible zero failure rate. Then a verdict field that a
customer-supplied check can populate, growing out of `tally.evals.FormatAdherenceEvaluator` rather than
from nothing. Ship the verifier's own accuracy as a first-class, nullable, measured quantity from day
one, because every downstream reliability number is bounded by it and a system that hides β will
overstate itself exactly the way the thesis's tables do.

Done when: a tenant can see a real pass rate per feature tag with a confidence interval, blanked with a
reason where the sample is thin, and the reported reliability of any verifier-gated claim carries the
verifier's own interval alongside it.

### P2: the taxonomy becomes a decision

Scope: enforce what the code already contemplates. Pass `known_feature_tags` to `SpanValidator`, which
is written and never called. Enforce `OPERATIONS` the way `_SAMPLING_STRATA` is enforced, or delete it
and stop implying an enum. Give a tenant a way to declare their task taxonomy through the control plane
the way every other per-tenant config works. Report per-tag variance so a customer can see which of
their tags are too coarse, which is the actual product Layer 2 describes.

Done when: a tenant has a declared task taxonomy, unknown tags are surfaced rather than silently
accepted, and the dashboard can show variance per task type.

### P3: repeat rate, reported and not served

Scope: a request-shape hash computed in the customer's process, a repeat rate per task type derived
from hashes alone, and no cache. Reuse `duplicated-work.ts`'s hard-won narrowing rather than
reintroducing the rule it replaced.

Done when: a customer can see what share of their spend on each task type goes to inputs they have
already answered, with the multi-turn caveat stated in the finding itself.

### P4: scheduled replay, and only then a frozen set

Scope: register replay and eval as scheduler jobs, which the engine already supports. Separately, and
harder, decide the frozen set's retention tier, its consent, and whether it is one set or two. Do not
freeze anything until `retention_days` is actually enforced somewhere, because a frozen set governed by
an unwritten bucket policy is not frozen.

### P5: cascade routing

Everything genuinely new lives here: a verifier registry, cascade execution in the request path,
conditional success storage, an exploration arm, and the recommendation surface. It is a different
product from cost observability and it should not start until P0 through P2 have produced numbers
someone trusts.

### The smallest first step

**P0, and inside P0 the single smallest useful piece is putting a real request and response into the
opt-in replay envelope.** Everything else in the thesis is downstream of it, it requires no new
architecture, it reuses the consent, retention, budget, floors and interval that already ship, and the
customer-visible result is that `wrong-sized-model.ts`, a surface that already refuses to emit a number
it cannot defend, starts telling the truth about a real candidate instead of about a blake2b hash.

The cheapest customer-visible result overall is P3's repeat-rate report, which needs no bodies and no
new consent. It is a good thing to ship in parallel and it unblocks nothing, so it should not be
mistaken for the first step.

## How this relates to the agent-to-agent initiative

`docs/specs/initiative-agent-to-agent.md` is downstream of everything here and is, in effect, this
thesis's distribution channel. Its `explain_saving` tool already promises "the incumbent's measured
per-call cost from real traffic, the candidate's replayed per-call cost, the pairwise win-rate with its
confidence interval, the number of samples judged, and the replay fidelity caveat", and its D3 says a
savings figure must be derivable from observed data. A cascade recommendation is exactly the proposal
shape that spec describes, with one addition it does not currently anticipate: a cascade proposal
changes reliability as well as cost, so the proposal object needs a reliability delta with its
interval, and that delta must carry the verifier's own accuracy or it is not a claim a human can check.

Two of that spec's statements need revising in light of this analysis. Its section 7 asserts "No bodies
in telemetry. Nothing here reads or returns prompts, completions or retrieved text", which is true of
the tools it lists and would stop being true the moment `explain_saving` returned an example of a
failed output, which is the first thing a human approver will ask for. And its D5, that the injection
surface is small "by construction rather than by filtering" because ai-tally stores no prompts or
completions, is already narrower than stated given `replay_runs.ResponseText`, and would narrow further
under any of the resolutions in this document. Both are fixable by saying so precisely, and both get
worse if left as written while this work proceeds.

The dependency runs the other way too. That spec is honest that it is "mostly assembly" and depends on
"replay and eval, which is what makes a savings claim checkable at all". Given that replay and eval are
currently mock-fed, P1 of that initiative would ship an agent-facing tool returning a judged win rate
produced by a hash function. P0 above is a prerequisite for it, not a parallel track.

## Open questions and risks

1. **The corpus row count is unverified.** The local ClickHouse refused the default credential during
   this investigation, so 903,081 rows at 53.82 MiB is reported and not confirmed. The byte-per-row
   arithmetic is consistent with an index-only table, which is what the code implies, but it is
   inference.
2. **Whether the replay envelope was ever intended to carry bodies.** `SampleCandidate.envelope`
   documents a full resolved envelope and the production caller supplies counts. That is either an
   unfinished path or a deliberate narrowing, and the commit history was not read far enough to say
   which. It changes whether P0 is a fix or a feature.
3. **β for any real verifier.** Every reliability figure in this document is parameterised by it and
   nobody has measured one. Until a single verifier's false-negative rate is bounded on real data, the
   cascade claim cannot be sized, only shaped.
4. **Whether customers will author verifiers.** The whole of Layers 4 and 5 assumes a customer will
   write and maintain a deterministic check per task type. That is a real engineering ask in their
   codebase, and unlike a model swap it cannot be proposed as a diff by an agent with any confidence,
   because the check encodes domain knowledge the agent does not have.
5. **Latency budget in the request path.** Verification and escalation are serial, in-process, and in
   the customer's hot path. No budget for this exists and the edge proxy's current design is a bounded
   tee that adds nothing measurable. What p95 regression a customer will accept for a 15x cost
   reduction is unknown and is probably the deciding question for the whole thesis.
6. **Whether the sampling budget is per-tenant or amortised.** Task types are tenant-specific, so the
   matrix is per-tenant, which is the version that does not scale. If task types are canonical across
   tenants the matrix is shared and the economics transform, but a canonical taxonomy across tenants is
   a much harder product problem than the thesis's Layer 2 describes and it has cross-tenant data
   implications the agent-to-agent spec explicitly rules out.
7. **What the frozen set does about a model that is withdrawn.** Layer 7's cold-start argument depends
   on replaying a frozen set against a newly released model. The symmetric case, a model being retired
   mid-window, breaks the time series and no policy exists for it.
8. **Whether CLAUDE.md should be amended.** As written, its no-bodies statement does not describe the
   system, because `replay_runs.ResponseText` is an authorised exception with its own consent. Leaving
   the discrepancy in place means every future reviewer relitigates it from first principles.
