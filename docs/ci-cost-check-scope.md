# Scope: CI cost check (GitHub Action)

**Status: proposal, not built.** A GitHub Action that comments on a pull request with the projected
cost impact of the change: "this PR raises cost per request 22 percent."

The strategic case is that this reaches teams who will never install a dashboard. It lands in the
workflow they already have, and the artifact is a PR comment rather than a login.

## The "80 percent done" claim, checked

Mostly true, for a narrower set of changes than it first appears.

`POST /v1/replay/estimate` already does the hard part. It accepts a `candidate_model`
(`{provider, model}`) and an optional `system_prompt_override`, replays them against a sample of
captured production envelopes through the same executor as `/v1/replay`, and returns a projection
with per-corpus extrapolation plus an honesty and diagnostics block.

Behind it: `sdk/python/src/tally/projection.py` is a real, tested engine (`test_projection.py`,
`test_replay.py`) that reports p99 cost per run rather than the mean, on the reasoning that a change
can leave the average flat while fattening the tail. `replay_sampler.py` builds the tail-weighted
sample, and `tenant_replay_config` already carries a `daily_budget_usd` cap.

So for the two most common cost-affecting changes, a model swap and a system prompt edit, the
projection machinery genuinely exists and is exercised. What does not exist is everything between a
git diff and that endpoint.

One caveat on maturity: the `/estimate` page is currently hidden from the nav because it renders mock
fixtures, and `web/lib/estimate.ts` is explicitly mock data. The engine is real; the surface over it
is not. This feature would be the first real consumer.

## The honest limitation, stated up front

The estimator models **declarative** changes. It cannot model **behavioral** ones.

`candidate_model` and `system_prompt_override` describe a different way of making the same calls. A
PR that adds a retry loop, introduces a second LLM call, raises `top_k` from 5 to 20 in a retriever,
or changes an agent's stopping condition changes the *number and shape* of calls, and no prompt
override expresses that. Projecting it properly would mean executing the PR's code, which is a
substantially larger feature and a different security posture.

This matters because the changes most likely to blow up cost are behavioral. An agent that loops
twice as often is the classic incident, and v1 would miss it.

The right response is not to hide this. The comment should say what it analysed and what it could
not:

> Analysed: system prompt change on `research_agent`.
> Not analysed: 3 other changed files that may affect call volume.

A cost check that silently ignores half a diff and prints a confident number is worse than no check.

## Decision 1: which changes are in scope for v1

| Change | Expressible today | In v1 |
| --- | --- | --- |
| Model swap (`gpt-4o` to `gpt-4o-mini`) | `candidate_model` | Yes |
| System prompt edit | `system_prompt_override` | Yes |
| Prompt template edit (Jinja, YAML, registry) | Same, once extracted | Yes if extractable |
| `max_tokens` / sampling parameter change | Not today, small addition | Stretch |
| Retrieval `top_k`, retry policy, agent control flow | Requires executing the PR | No |

Recommendation: ship the first two, detect the rest and say they were not analysed.

## Decision 2: extracting intent from a diff

This is the genuinely new work, and the part most likely to be underestimated.

Something has to turn "these 14 files changed" into "the system prompt for `research_agent` changed
from A to B". Prompts live in string literals, Jinja templates, YAML, JSON, or a prompt registry, and
model names live in config, environment variables, or inline calls. A general solution is a research
project.

Three approaches:

**Declared, via a config file** (recommended). The repo carries `.tally-ci.yml` naming where prompts
and models live:

```yaml
features:
  research_agent:
    system_prompt: prompts/research_system.txt
    model_config: config/models.yaml#research.model
```

Explicit, boring, debuggable, and it fails loudly when a path moves. The cost is that a team must
write it, which is a real adoption tax on a feature whose whole pitch is low friction.

**Convention.** Scan for `prompts/*.txt`, known SDK call sites, common config keys. Zero setup, and
wrong often enough to erode trust.

**SDK-assisted.** The SDK already knows the resolved prompt at runtime and could emit a stable
identifier tying a feature tag to a source location. Best long-term answer, and it needs the prompt
hash wiring described in `docs/waste-detection-scope.md`, which is currently an orphan column.

Recommendation: config file for v1, convention-based detection as a suggestion that generates the
config file on first run, SDK-assisted later.

## Decision 3: baseline, and what "22 percent" means

`/v1/replay/estimate` projects one candidate. CI needs a comparison, and the baseline choice changes
the number.

Options: replay both the base branch's prompt and the PR's prompt over the same sample (a true
paired comparison, twice the replay cost), or compare the PR's projection against observed production
cost for that feature (cheaper, and it conflates the change with drift in traffic mix).

Recommendation: paired replay over one shared sample. It is the only version where "22 percent" is
attributable to the change rather than to the week. It also halves the variance, since both arms see
identical inputs.

The headline should be **cost per request**, not projected monthly cost. Per-request is a property of
the change; monthly depends on volume the PR author does not control. Show projected monthly as a
secondary line for context.

Report p99 alongside the mean, following the engine's existing posture. A change that leaves the mean
flat and doubles p99 is exactly the change worth catching, and a mean-only comment would pass it.

## Decision 4: significance, and not crying wolf

With a sample of 50 replayed runs, a 3 percent difference is noise. The comment must not report it as
a finding.

The engine already computes a bootstrap and a Wilson interval, so the honest output is a delta with a
confidence interval, plus an explicit "no significant change detected" state when the interval spans
zero. `/v1/replay` already treats fewer than 50 replayed responses as too thin to report on, and that
threshold should carry over.

Three outcomes: significant increase, significant decrease, no detectable change. Anything else is
false precision.

## Cost of running it

This feature spends real money on every run, which makes it unlike any other CI check.

Each PR check replays N production envelopes against a live model, twice for a paired comparison.
`tenant_replay_config.daily_budget_usd` defaults to **$5.00**, which at realistic per-call costs is a
handful of checks per day. That default was chosen for a manual workflow, not for something firing on
every push.

Controls needed:

- **Cache by content hash.** The same prompt and model pair on a re-push should reuse the prior
  result. Most PR pushes do not change the prompt, so this alone removes most of the spend.
- **Debounce.** Run on PR open and on pushes that touch declared paths, not on every commit.
- **A per-repo daily cap**, surfaced in the comment when exhausted ("skipped, daily replay budget
  reached") rather than silently not commenting.
- **Sample size as a knob**, traded off against confidence width.

The comment should state what the check itself cost. A tool that reports on spend and hides its own
is not credible.

## The Action

A composite action:

```yaml
- uses: ai-tally/cost-check@v1
  with:
    api-key: ${{ secrets.TALLY_API_KEY }}
    config: .tally-ci.yml
```

It resolves the changed files, reads the config, extracts baseline and candidate prompt or model,
calls `/v1/replay/estimate` twice, and posts a **sticky comment** updated in place rather than a new
comment per push.

A status check is worth offering but should default to non-blocking. A cost regression is
information, not a build failure, and a team can opt into failing above a threshold. Blocking merges
on a sampled projection by default would get the action uninstalled within a week.

Authentication is a tenant API key in repo secrets. Worth noting this is a second outbound-facing
surface, alongside the alerts destination in `docs/cost-alerts-scope.md`.

## The comment

Concrete, and short enough to read in a PR:

> **ai-tally cost check**
> Cost per request: **+22%** ($0.0141 to $0.0172), 95% CI +14% to +29%
> p99 per request: +31%
> Projected monthly at current volume: +$3,180
> Analysed: system prompt for `research_agent` (prompts/research_system.txt)
> Not analysed: 2 changed files that may affect call volume
> Based on 50 replayed production requests. Check cost: $0.38.

Every number in that comment is defensible or labelled. The "not analysed" line is what keeps it
honest.

## Phasing

1. **The config file format and diff extraction** for prompts and models.
2. **A CI-facing endpoint** that takes baseline and candidate and returns a paired comparison, so the
   action does not orchestrate two calls and compute statistics itself.
3. **The Action**: sticky comment, non-blocking status, auth.
4. **Caching, debounce and budget controls.** Before any wide rollout, not after.
5. **Detection and reporting of un-analysable changes.**
6. Parameter changes (`max_tokens`, sampling), then SDK-assisted extraction.

## Open questions

1. Does this require production traffic in ai-tally already? Yes today, since replay samples captured
   envelopes. That makes the "never install a dashboard" pitch partly circular: you need to be
   ingesting before CI checks work. Worth deciding whether a synthetic corpus is an acceptable
   fallback for a first-run experience, and being clear it is weaker evidence.
2. GitHub only, or GitLab and others? The projection work is shared; only the comment surface differs.
3. Who pays for replay spend on an open source repo where anyone can open a PR? Forked PRs must not
   be able to drain a budget or read secrets.
4. Does the check need per-feature scoping when one PR touches several features?
5. Should a decrease be celebrated as loudly as an increase? A comment that only ever appears with bad
   news gets read as a nag.

## Risks

**Circularity in the pitch.** The adoption story is that teams who would never install a dashboard
adopt this instead. But replay needs captured production traffic, which means the SDK or proxy is
already deployed. The action is a great *expansion* surface for existing tenants and a weaker
*acquisition* one than it first appears. Worth being clear-eyed about which it is.

**Missing behavioral regressions.** v1 cannot see a new retry loop, which is the most common way cost
actually blows up. If the comment reads as a general cost check rather than a prompt and model check,
it will be trusted for something it does not do.

**Noise gets it uninstalled.** A check that fires on every push with a different number, or blocks
merges on a sampled estimate, is removed quickly. Non-blocking by default, sticky comments, and a
real significance test are what keep it installed.

**Spend on CI.** Every check costs money against a budget that currently defaults to $5 a day. Without
caching and caps, a busy repo exhausts it before lunch and the feature looks broken.
