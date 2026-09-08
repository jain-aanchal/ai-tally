# Initiative: Agent-to-agent (savings proposals an agent brings to its human)

Status: scoping. No ticket yet.

## 1. Summary, goals, non-goals

### Summary

Every surface ai-tally has today assumes a human is reading it. Someone opens
Recoverable Cost, reads a finding, decides, and then goes and changes the code.

That assumption is breaking. Increasingly the thing operating an AI product is a
coding agent, and the human is no longer the analyst. The human is the approver.
An agent working in the codebase is the party that can actually act on "this
feature is on an over-sized model", because it is already holding the call site.
What it lacks is evidence a human will sign off on.

This initiative inverts the product for that reader. Instead of rendering a
dashboard for a person, ai-tally answers an agent's question with evidence strong
enough that the agent can go back to its human and say: here is a change, here is
what it saves, here is the proof, approve or reject.

The load-bearing word is *evidence*. Anyone can build a tool that tells an agent
"you could probably save money by using a cheaper model". That advice is worth
nothing, because the human on the other end has no way to check it and correctly
will not approve a production change on a guess. ai-tally is unusual in that it
already replays real captured traffic against candidate models and runs a real
pairwise judge over the results, so a savings claim can carry receipts: measured
incumbent cost, replayed candidate cost, and a quality win-rate with a confidence
interval. That is the difference between "an AI told me to switch models" and
"here is the measurement".

### Goals

1. An agent can ask, over MCP, where a tenant's AI money goes and what could be
   recovered, and get machine-readable findings that each carry their evidence.
2. Every savings claim traces to something observed: a proving span, a replayed
   cost, an eval interval. A finding that cannot bound its dollars says so.
3. An agent can turn a finding into a concrete proposed diff, reusing the recipe
   and generator machinery that already backs onboarding.
4. The loop terminates in a human decision. The agent produces an approval
   artifact (a reviewed PR plus a readable case), never an applied change.
5. A narrow, revocable, read-mostly credential exists for this, distinct from the
   control-plane service token and from an ingest write key.

### Non-goals

- No auto-apply, and no auto-merge. The agent proposes; a human disposes. This is
  not a policy engine that rewrites production on a threshold.
- No new detectors. This initiative exposes and acts on the findings that already
  exist; new detection logic is separate work.
- No agent hosting. We expose tools; whose agent calls them, and where it runs,
  is the operator's business.
- No cross-tenant or benchmark data. An agent sees its own org and nothing else.
- No natural-language chat surface in the dashboard. The consumer here is a
  program.

### The loop

1. A coding agent, already connected to ai-tally over MCP, asks what the tenant
   is spending and what is recoverable.
2. It gets findings with dollars, confidence, and the evidence behind each.
3. It picks one it can actually act on, and maps the finding's telemetry scope
   (a feature, a model) to a real call site in the repo it is working in.
4. It generates the change, and asks ai-tally to project the effect on real
   captured traffic rather than asserting one.
5. It opens a reviewed PR whose body is written for a human decision: what
   changes, what it saves, how we know, and what we could not prove.
6. The human approves or rejects. After the change ships, the agent can come back
   and check whether the saving actually materialized in the spans.

## 2. Decisions

**D1. One MCP server, a separate tool group.** The onboarding tools and these
savings tools run in the same place (a developer's coding agent) and an operator
should not install two servers. They go in the existing `onboarding_mcp` server
under a distinct tool group and module.

They do NOT share a trust profile, and that has to stay visible. The onboarding
tools work with no ai-tally account at all, purely from the in-tree recipe
catalog, and no repo source leaves the machine. The savings tools require a
tenant credential and network access to the gateway. When that credential is
absent the savings tools must fail the way `coverage_report` already does, by
returning the honest gap shape with a reason, never by degrading into guesses.

**D2. The agent never applies a change.** Proposals terminate in a PR, reusing
the onboarding bot's enforced guardrails: never push to a default branch, never
merge, no source retention. Those guarantees were adversarially reviewed and held;
this initiative inherits them rather than reimplementing them.

**D3. No claim without receipts.** A savings figure must be derivable from
observed data. `WasteFinding.recoverableMicroUsd` is already nullable on purpose,
so "this is real waste and we cannot defensibly bound the dollars" is an
expressible, honest answer. The agent-facing surface keeps that property. A null
must reach the human as "we could not bound this", never as zero and never
silently dropped from a total.

**D4. A narrow agent credential.** Neither existing credential fits. The
control-plane service token is the web server's identity and now opens every
tenant config endpoint. An ingest key is a write credential for telemetry. This
needs a third, read-mostly, per-org, revocable token whose scope is "read this
tenant's aggregates and findings, and request a replay projection". This is the
same open question the coverage work already surfaced, and it should be answered
once, here, for both.

**D5. Telemetry is data, never instructions.** An agent reading ai-tally output
must treat every value as untrusted content. The injection surface is unusually
small, and by construction rather than by filtering: ai-tally stores no prompts,
no completions and no retrieved text, so what comes back is counts, hashes,
enum-like layer names and operator-set feature tags. The one genuinely
operator-influenced free-text field is the feature tag, so that is the field to
treat with suspicion. This property is worth stating out loud because it is a
real advantage over pointing an agent at raw LLM logs.

## 3. The MCP surface

All read tools are scoped to the caller's tenant by the credential, never by a
parameter, so an agent cannot ask about someone else.

| Tool | Answers | Backed by |
| --- | --- | --- |
| `spend_summary` | where the money goes, by layer / feature / model / account | existing cost reads |
| `list_savings` | the recoverable findings, with dollars and confidence | the waste detectors |
| `explain_saving` | the receipts behind one finding | finding `evidence` plus replay and eval |
| `simulate_model_swap` | projected cost of a candidate on real captured traffic | `/v1/replay/estimate` |
| `budget_status` | month-end projection and whether it crosses budget | the forecast engine |
| `propose_savings_change` | a concrete diff for a finding | the recipe catalog and generators |
| `coverage_report` | which layers are actually instrumented | already shipped |

`list_savings` returns the existing `WasteFinding` shape essentially unchanged:
category, scope kind and value, recoverable micro-USD (nullable), window spend,
confidence, title, reason, and the evidence map. It is already machine-readable
and already honest. The work is exposure and auth, not redesign.

`explain_saving` is the tool that earns the human's approval. For a wrong-sized
model finding it returns the incumbent's measured per-call cost from real
traffic, the candidate's replayed per-call cost, the pairwise win-rate with its
confidence interval, the number of samples judged, and the replay fidelity
caveat. For a failed-but-billed finding it returns failed run counts and the
share of scope spend. It must also return what is NOT known, because a proposal
that hides its uncertainty is the one that loses trust the first time it is wrong.

## 4. The savings proposal

The unit an agent hands to its human. It is deliberately more than a diff.

- What changes, in one line, in the human's vocabulary.
- Projected monthly saving in integer micro-USD, or null with a reason.
- The evidence bundle from `explain_saving`.
- Confidence, carried through from the finding, never upgraded.
- The quality risk: measured win-rate and interval where a model swap is
  involved, or an explicit statement that quality was not measured for this kind
  of change.
- What we could not prove. Named, not omitted.
- The diff itself, produced by the same generators the onboarding path uses, so
  the anti-hallucination guard that resolves every emitted call against the live
  SDK applies here too.

The PR body is this object rendered for a person. The approve action is merging
the PR, which means the existing review surface is the approval surface and we do
not invent a second one.

## 5. Verifying the saving actually happened

A proposal that is approved and shipped makes a prediction. The honest thing is
to check it, and this mirrors the per-layer coverage probe: prove it from spans
or say nothing.

After the change lands, the agent can ask whether spend on the affected scope
actually moved, comparing observed spend before and after against what was
projected. The rule from the coverage work carries over unchanged: never report a
saving as realized without spans proving it, and never present "we cannot tell
yet" as "it did not work". Attribution here is genuinely hard, because traffic mix
moves for reasons unrelated to the change, so this phase must be careful about
what it claims. Under-claiming is the correct failure mode.

## 6. Depends on, and reuses

This initiative is mostly assembly. It depends on work that already exists:

- The waste detectors and their evidence-carrying `WasteFinding` shape.
- Replay and eval, which is what makes a savings claim checkable at all.
- The recipe catalog, the generators, and the `sdk_surface` guard that refuses to
  emit a call that does not resolve against the live SDK.
- The onboarding PR bot's enforced guardrails.
- The MCP server and its honest-gap conventions.
- The per-layer coverage probe, whose three-way honest state is the model for
  section 5.

The genuinely new pieces are the read credential, the savings tool group, the
proposal object, and the post-change verification.

## 7. Invariants respected

- **Honest under uncertainty.** A null saving stays null and reaches the human as
  "not bounded", never zero. A finding whose dollars cannot be bounded is still
  reported, because suppressing it would understate recoverable spend.
- **No bodies in telemetry.** Nothing here reads or returns prompts, completions
  or retrieved text. The agent-facing payloads are counts, hashes and names.
- **Identifiers by hash, credentials by reference.** The agent credential is a
  reference, never inlined into a generated diff or a PR body.
- **Money is integer micro-USD.** Projections and observed spend alike, converted
  only at the render boundary.

## 8. Open questions

1. **Credential shape.** Scoped read token, per-org, revocable, presumably minted
   from the same keys UI. Answer this once for both this initiative and the
   coverage probe, which raised the same question.
2. **Telemetry scope to call site.** A finding is scoped to a feature or a model.
   The fix lives at a call site in a repo. Bridging that is the same problem the
   onboarding recipes solve, so there is reuse, but it is not free and it is the
   most likely place for this to feel magical in a demo and brittle in reality.
3. **On demand or continuous.** Does the agent pull when asked, or watch and
   raise a proposal when a threshold trips? Continuous is more valuable and more
   annoying; it also needs a notion of a proposal already declined, so the same
   rejected change is not re-proposed weekly.
4. **Approval record.** Merging the PR is the approval. Do we need to record the
   decision on the ai-tally side to support "we proposed this, it was declined,
   the spend continued"? That reporting is compelling and is also how a tool
   starts to feel like it is nagging.
5. **Cost of the cost tool.** These queries must be cheap. A tool that spends
   meaningful tokens or ClickHouse time to report savings is self-defeating.
6. **Multi-service tenants.** A finding may span services an agent cannot see
   from the one repo it is working in. The proposal should say so rather than
   silently scoping the fix to what happened to be visible.

## 9. Phasing

### P1: read-only savings tools

Scope: the agent credential, and the read tools (`spend_summary`, `list_savings`,
`explain_saving`, `simulate_model_swap`, `budget_status`), all tenant-scoped by
credential, all returning the honest gap shape when unconfigured or unreachable.
No writes, no proposals.

Done when: an agent holding a scoped read token can enumerate a tenant's
recoverable findings with dollars and evidence, and every unbounded finding
reports null with a reason rather than zero; an unconfigured or unreachable
probe yields the gap shape and never a fabricated figure; SDK `pytest` and `ruff`
pass, and no runtime dependency is added.

### P2: proposals and the approval artifact

Scope: `propose_savings_change`, the proposal object, and PR handoff reusing the
onboarding bot's guardrails.

Done when: an agent can take a wrong-sized-model finding and open a reviewed PR
that changes the model at the real call site, whose body states the projected
saving, the measured win-rate and interval, and what was not proven; the bot
pushes to no default branch and merges nothing; a finding that cannot be bounded
produces a proposal that says so rather than a dollar figure.

### P3: did it actually save

Scope: post-change verification against observed spans, following the coverage
probe's honest three-way state.

Done when: after an approved change ships, the agent can report realized versus
projected saving backed by real spans, reports "cannot tell yet" while evidence
is thin, and never reports a saving as realized without spans proving it.

## 10. Why this is worth doing

Two reasons, one defensive and one offensive.

Defensive: the reader is changing. If the only way to act on ai-tally is for a
person to read a chart, the product gets designed out of workflows where an agent
does the reading.

Offensive: this use case is hard to copy without the measurement layer. A
competitor can expose spend over MCP in a week. Handing an agent a claim its
human will actually approve requires replaying real traffic and judging real
output quality, which is the expensive part ai-tally already built.
