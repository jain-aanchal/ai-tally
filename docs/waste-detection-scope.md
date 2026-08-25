# Scope: waste detection

**Status: proposal, not built.** Find the spend that produced nothing: the gap between "here is your
bill" and "here is the $3k a month you are burning for no reason".

This is the most demo-able idea in the product. It is also the one most able to damage trust,
because every number it prints is a claim about money you could have saved, and that claim is a
counterfactual. The scope below is organised around that tension.

## The honesty problem, first

"We found $4,200 a month of pure waste" is not a measurement. It is a prediction about what would
have happened under a different setup. Some of these categories can be measured almost exactly
(cache savings left on the table), and some cannot be known without changing the application
(whether a retrieved chunk influenced an answer).

If the tab presents all five with equal confidence, the weakest number defines the credibility of
the whole feature. The first customer who acts on a $4,200 estimate and saves $600 will not trust
the dashboard again.

So the design rule: **every waste figure carries its assumption and a confidence tier**, and the
headline sums only what we can defend.

- **Measured.** Arithmetic on data we already have. Example: tokens billed at full rate that a cache
  would have served, priced from the catalog.
- **Estimated.** Real signal, modelled saving. Example: near-duplicate requests, where the saving
  depends on a cache hit rate we assume.
- **Indicative.** A smell worth investigating, no dollar claim. Example: a system prompt that grew
  40 percent last week.

A headline that reads "$2,100 measured, $2,100 more estimated" is both more honest and more
persuasive than "$4,200", because the first number survives scrutiny.

## What the pipeline can support today

I checked each of the five against the actual schema and SDK. The results differ a lot, and this
should drive the build order.

| Category | Signal today | Verdict |
| --- | --- | --- |
| Prompt cache hit rate | `otel_spans.CachedInputTokens` exists **and is mapped** by the gateway; SDK emits `gen_ai.usage.cached_input_tokens` | Buildable now |
| Retry and failed-call spend | `StatusCode` exists; `AgentRunId` / `AgentStepIndex` exist. **No retry attribute** anywhere in the SDK | Partly buildable, needs SDK work |
| Oversized system prompts | Only `InputTokens`, a single total. No prompt segmentation | Needs SDK work |
| Unused retrieved chunks | `ResolvedContextRef` exists in schema and SDK, but nothing records which chunks influenced the answer | Research, largest gap |
| Near-duplicate requests | `ResolvedPromptHash` column exists in ClickHouse but **nothing writes it**: it is absent from the SDK schema and from `gateway/mapping.py` | Orphan column, needs wiring |

Note the demo tenant currently has all of these at zero (`CachedInputTokens`, `StatusCode != 0`,
`ResolvedPromptHash`, `ResolvedContextRef` are unpopulated across 281,094 spans), because the
backfill does not emit them. So even the buildable category has no data to show until real traffic
or a richer backfill exists.

## The invariant this feature keeps bumping into

The product does not put bodies in telemetry. No prompts, no completions, no retrieved text. Counts
and hashes only, with PII scrubbed. That is a core promise, and it is exactly what three of these
five categories want to inspect.

The resolution is to compute the signal **where the content already is** (in the SDK, in the
customer's process) and emit only a derived number:

- Oversized system prompt: the SDK measures the system-prompt token count and emits an integer. We
  never see the prompt.
- Near-duplicate: the SDK emits a hash of the normalised prompt. Exact duplicates are detectable
  from hash collisions alone, with no text leaving the customer.
- Unused chunks: the SDK emits chunk ids and, if the application can supply it, which ids were cited.
  Ids, not text.

This is more SDK work than dashboard work, which is the opposite of how this feature is usually
imagined. Worth being clear about that up front: **waste detection is mostly an instrumentation
project**, and the tab is the last 20 percent.

## The five categories

### 1. Prompt cache hit rate and savings left on the table

The strongest one, and the right place to start. `CachedInputTokens` already flows end to end.

Two numbers, both defensible:

- **Realised saving.** Cached tokens billed at the cached rate instead of full input rate. Priced
  from the existing catalog, which already carries cache pricing per model. This is measured.
- **Saving left on the table.** Input tokens that were re-sent uncached but appeared in a prior call
  within the provider's cache TTL. Requires knowing the repeated prefix, which needs the prompt hash
  from category 5. Estimated, and the assumption (TTL, minimum cacheable prefix) must be stated.

Provider caveat that has to be modelled honestly: cache semantics differ per provider (minimum
prefix length, TTL, explicit versus automatic). A single blended rule will be wrong for someone.

### 2. Retry and failed-call spend

Money for zero output. Conceptually the cleanest waste there is.

Failed calls that still consumed tokens are computable today from `StatusCode` plus token counts.
Retries are not: nothing marks a call as attempt N of a sequence. `AgentRunId` and `AgentStepIndex`
give ordering within an agent run but do not distinguish "retried the same call" from "took the next
step".

Needs a small SDK addition: an attempt number and a retry-of reference. With those, "spend on calls
that ultimately failed" and "spend on superseded attempts" become measured numbers.

Subtlety worth stating on screen: a retry that eventually succeeds is not pure waste, it bought
reliability. The honest framing is "spend on attempts that produced no output", not "money wasted".

### 3. Oversized system prompts sent on every call

High value because it multiplies: 2,000 wasted tokens on every one of a million calls is real money.

Not computable today. `InputTokens` is one number with no breakdown. Needs the SDK to emit a token
count per prompt segment (system, context, user), which is cheap to compute where the prompt is
assembled.

With that, the tab can show system-prompt tokens as a share of all input tokens, the trend over time
(a system prompt that grew is a specific, actionable finding), and the cost of the fixed prefix at
current volume. The saving claim is Estimated: we can price the tokens exactly, but only a human can
say which parts of the prompt are unnecessary. The right output is "your system prompt costs $890 a
month at current volume", not "you are wasting $890".

### 4. Retrieved chunks that never influenced the answer

The most valuable and the least tractable. RAG pipelines routinely retrieve 20 chunks and use two,
paying input tokens on all 20.

The blocker is that influence is not observable from outside the model. Options, none clean:

- **Citation-based.** If the application already tracks which chunks were cited, it emits those ids.
  Accurate, and only works for apps that cite.
- **Position and rank heuristics.** Cheap, and weak enough that a dollar claim is not defensible.
- **Attribution probing.** Re-run without a chunk and compare. Accurate, expensive, and it doubles
  spend to measure waste, which is a hard sell.

Recommendation: v1 emits chunk count and retrieved-context token cost, and reports it as Indicative,
"you spend $X a month on retrieved context, of which N chunks per call". No savings claim. Take the
citation path only for tenants who can supply citations. Resist the temptation to guess here: this is
the category most likely to produce a confident wrong number.

### 5. Near-duplicate requests a semantic cache would kill

`ResolvedPromptHash` is an orphan column: it exists in ClickHouse but no writer populates it. Wiring
it is the unlock for both this category and the "left on the table" half of category 1.

Two tiers:

- **Exact duplicates.** Same normalised prompt hash within a window. Purely measured once the hash
  flows. Strong finding: the same question asked 4,000 times a month at $0.02 each is a number nobody
  argues with.
- **Semantic near-duplicates.** Requires embedding prompts and clustering by distance, which means
  either embedding in the SDK (cost and latency in the customer's hot path) or receiving prompt text
  (violates the invariant). Neither is free.

Recommendation: ship exact-duplicate detection, which is cheap and defensible, and treat semantic
clustering as a separate proposal. The demo lands fine on exact duplicates.

## The page

Route `/waste`, in the nav. Structure that follows the honesty rule:

Headline: total identified waste, **split into measured and estimated**, over the last 30 days, with
the equivalent annual figure. Under it, a plain sentence naming what is excluded.

Then one card per category, each with: the dollar figure, its confidence tier, the assumption in one
line, how many calls or tokens it covers, and a concrete next action ("enable prompt caching for
claude-sonnet on the research agent"). A finding without an action is trivia.

Each card links to the underlying spans so a sceptical engineer can check the arithmetic. This
matters more here than anywhere else in the product: the first question any good engineer asks of a
"$4,200 wasted" claim is "show me".

## Phasing

1. **Wire `ResolvedPromptHash`** through SDK and gateway. Small, and unblocks two categories.
2. **Exact-duplicate detection** and **prompt cache realised savings**. Both measured, both shippable
   on existing plus newly wired signal. This is the demo.
3. **SDK: prompt segment token counts and retry or attempt markers.** The instrumentation half.
4. **Oversized system prompt and retry or failure spend cards.**
5. **Retrieved-context reporting**, Indicative only.
6. Semantic near-duplicates and citation-based chunk analysis, each as its own proposal.

Phases 1 and 2 are the minimum that produces a credible demo. Everything after that widens coverage.

## Open questions

1. Does the headline sum measured only, or measured plus estimated? Recommendation is measured, with
   estimated shown beside it. This is a product positioning call as much as a technical one.
2. Do we need a richer demo backfill? Today's demo data has zero cache, zero errors and zero prompt
   hashes, so the tab would be empty in exactly the demo it is meant to sell.
3. How much SDK overhead is acceptable in the customer's hot path? Hashing a prompt is cheap;
   embedding one is not.
4. Do waste findings feed the alerts feature (`docs/cost-alerts-scope.md`), for example "notify me
   when cache hit rate drops below 40 percent"? That is a natural pairing and worth designing for.
5. Is waste scanned continuously or on demand? Continuous needs the scheduler that three features now
   want.

## Risks

**Overclaiming.** The single biggest risk. A headline number that does not survive a customer's own
analysis costs more trust than the feature earns. The confidence tiers are the mitigation and they
should not be dropped for a cleaner headline.

**It is an instrumentation project wearing a dashboard costume.** Most of the work is in the SDK, and
the value only appears after customers upgrade and redeploy. Plan for the lag.

**Empty for existing tenants.** Like cost per customer, this ships dark until the new signals flow.
The cache category is the exception, and it is the reason to start there.

**Provider-specific correctness.** Cache rules, retry semantics and token accounting differ per
provider. A blended model will be confidently wrong for someone, which is worse than being narrow
and right.
