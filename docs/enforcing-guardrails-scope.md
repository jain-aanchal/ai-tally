# Scope: guardrails that stop things

**Status: proposal, not built.** A budget ceiling per feature and per tenant that can downgrade the
model or refuse the call.

## One correction: refusal already works

The premise is that OBSERVE mode exists and enforcement is the missing half. Enforcement is further
along than that. `sdk/python/src/tally/guardrails.py` already implements all four modes end to end:

- `OBSERVE` records what would have fired and always proceeds.
- `WARN` proceeds and returns a warning the agent can act on at `warn_at` (default 80 percent).
- `GRACEFUL` raises a localized `CostLimitExceededException` for the framework to catch and return a
  degraded response.
- `HARD_STOP` refuses, opt-in, documented as being for idempotent or read-only agents.

It tracks `cost`, `steps` and `tool_calls`, emits verdicts as span attributes
(`enforced` / `shadow_observed` / `passed`), and the `wouldHaveFiredThisWeek` count is already the
graduation signal in the UI. The control plane (`tenant_guardrails`) carries a `cost_cap` kind and
defaults `state` to `shadow`.

So a per-run cost cap that refuses a call is built. Three specific things are missing, and they are
what this scope is about.

## What is actually missing

### 1. Scope: caps are per-run, not per-budget

`GuardrailState` is keyed on `trace_id` and counts within one process. The module says so plainly:
"v1 is single-process (counters are per-process)", with a shared counter deferred to CTO-83.

That means today's cap answers "this *run* must not exceed $2", not "this *feature* must not exceed
$5,000 this month". A budget ceiling is a shared, durable counter over a calendar window, across
every process and machine serving that feature. Nothing in the codebase holds that state.

This is the bulk of the work, and it is a distributed systems problem rather than a rules problem.

### 2. There is no downgrade action

The engine's outcomes are proceed, warn, or raise. There is no path that substitutes a cheaper model
and continues. Grepping for a downgrade or fallback model turns up nothing outside Stripe plan
changes. This is genuinely new behavior, and it is the more commercially interesting half: refusing a
call breaks a product, while serving Haiku instead of Sonnet at 90 percent of budget usually does not.

### 3. Enforcement is Python only

The guardrail engine lives in the Python SDK, so it protects Python callers and nobody else. The Go
edge proxy (`infra/edge-proxy/`) sits in the request path for every language and currently does not
enforce anything. A tenant using the proxy from TypeScript has no cap available.

## Your design instinct is already the codebase's

OBSERVE as default with ENFORCE as explicit opt-in is not a change to argue for. It is what is built:
`GuardrailConfig.mode` defaults to `OBSERVE`, the `tenant_guardrails.state` column defaults to
`shadow`, `GUARDRAIL_MODES` carries an explicit `enforcing` flag, and the UI is organised around a
graduation ladder from observe to enforcing. This scope keeps all of it. The new budget rules should
default to shadow and require a deliberate flip, exactly like the existing ones.

## Decision 1: where the shared counter lives

A budget cap needs a counter that every process can read and increment cheaply, checked before an
outbound call.

| Option | Latency | Durability | Notes |
| --- | --- | --- | --- |
| ClickHouse aggregate | Too slow for a hot path | Good | Fine for reporting, wrong for a gate |
| Postgres row with an atomic increment | Tens of ms, contended | Good | Simple, becomes a bottleneck at volume |
| Redis counter with periodic reconcile | Sub-ms | Weak alone | Standard answer for rate and budget limits |
| Gateway-held counter | One network hop | Medium | Reuses the existing per-tenant rate limiter |

Recommendation: a Redis-style counter owned by the gateway, incremented from observed spend, and
reconciled against ClickHouse periodically so drift does not accumulate. The gateway already runs a
per-tenant rate limiter, so the shape is familiar.

The counter is necessarily approximate. Cost is known only after a call returns, so a check before
the call is always reading a slightly stale number. A budget cap will overshoot by roughly one call
per concurrent worker, and the docs should say so rather than implying a hard ceiling.

### The window

Daily, weekly and monthly windows, with the same late-data caveat as
`docs/cost-alerts-scope.md`: connector-sourced compute and egress arrive on a daily pull and are
about 47 percent of spend on the demo tenant. A budget counter fed only by live LLM spend is
measuring half the bill. Either the cap is explicitly an **LLM spend cap** (honest, simple,
recommended for v1) or it waits on infra cost that arrives a day late, which cannot gate a live call.

Call it what it is. "LLM spend cap" is defensible; "budget cap" that silently ignores half the bill
is not.

## Decision 2: what downgrade means

The most valuable new action, and the one with the most product risk.

A downgrade rule needs a substitution map: at breach, `claude-sonnet-4-5` becomes
`claude-haiku-4-5`. Per feature, tenant-configured, because only the tenant knows which substitutions
are acceptable for their workload.

Three things this must get right:

**It is a quality change, not just a cost change.** An end user gets a worse answer. That is usually
better than an error, but it must not be invisible. The substitution should be recorded on the span
(`gen_ai.guardrail.downgraded_from`) and returned to the caller so the application can disclose it if
it wants to. Silently serving a cheaper model and reporting nothing would be the wrong default for a
product whose posture is honesty about what it does and does not know.

**Tiering beats a cliff.** The useful shape is graduated: warn at 80 percent, downgrade at 100
percent, refuse at 120 percent. That gives a team a soft landing instead of a wall, and it is the
version people will actually enable. The existing `warn_at` field is the seed of this.

**Model availability is not guaranteed.** The target model must exist for the provider and be priced
in the catalog. `models.py` already does discovery and family classification, so validate the
substitution at config time rather than discovering it at breach time in production.

## Decision 3: fail-open, and this one is not close

If the counter is unavailable, does the call proceed?

**Fail open.** An observability tool must never take down a customer's product. If Redis is down or
the gateway is unreachable, calls proceed uncapped and the incident is recorded loudly. A spend
overrun is recoverable; an outage caused by the monitoring layer ends the vendor relationship.

The only defensible exception is a tenant explicitly opting into fail-closed for a workload where
runaway spend is worse than downtime (a batch job, an internal tool). That should be a per-rule flag,
off by default, with the consequence spelled out in the UI.

This should be stated in the docs and on the settings screen, because a customer buying a "spend cap"
will assume it is a hard guarantee, and it is not.

## Decision 4: where enforcement happens

**SDK.** Already there, richest context (it knows the agent run, the step count), Python only.

**Edge proxy.** Language-agnostic and sees every call, so it is the only way to cap a TypeScript or
Go customer. It is also in the latency path for everyone, and the repo already has an
`overhead_test.go`, so the bar for added latency is established.

Recommendation: both, sharing one counter and one config. The proxy is the enforcement point that
makes this a product rather than a Python library feature, and it is where the "teams will pay for a
spend cap" argument actually gets paid. Ship SDK first because the engine exists, then the proxy.

Latency budget matters: a cached counter read with a short TTL, not a synchronous round trip per
call. Overshoot slightly rather than adding tens of milliseconds to every request.

## Data model

Extend `tenant_guardrails` rather than inventing a parallel system. The `params` JSONB already
absorbs rule-specific config, and `kind` already allows `cost_cap`.

```
kind = 'budget_cap'
params = {
  "window": "month",                 -- day | week | month
  "amount_micro": 5000000000,        -- $5,000
  "scope_kind": "feature",           -- tenant | feature | model
  "scope_value": "research_agent",
  "actions": [
    {"at_pct": 80,  "action": "warn"},
    {"at_pct": 100, "action": "downgrade",
     "map": {"claude-sonnet-4-5": "claude-haiku-4-5"}},
    {"at_pct": 120, "action": "refuse"}
  ],
  "fail_closed": false
}
state = 'shadow'                     -- default, as with every other rule
```

`tenant_guardrail_changes` already exists as an audit table, which matters more once a rule can
refuse production traffic. Who enabled the thing that broke checkout is a question someone will ask.

## Shadow mode has to be real here

For a budget cap, shadow is not a formality. It must answer "if I had enabled this last month, how
many calls would have been refused and which features would have degraded?" That is the number that
makes a team comfortable flipping the switch, and it is computable from history without enforcing
anything.

The existing `wouldHaveFiredThisWeek` is the right primitive. Extend it to report, per rule, the
count of would-be warns, downgrades and refusals, and the spend that would have been avoided. The
last figure is the sales argument.

## Phasing

1. **The shared counter** in the gateway, with periodic reconciliation. Nothing works without it.
2. **`budget_cap` rule kind** plus config UI on `/guardrails`, shadow only. No enforcement yet.
3. **Shadow reporting**: would-have-fired counts and avoided spend, per rule.
4. **Enforcement in the SDK**: warn, then refuse, reusing the existing modes.
5. **The downgrade action**, with substitution validation and span disclosure.
6. **Enforcement in the edge proxy**, which is what makes it language-agnostic.

Phases 1 through 3 are shippable without enforcing anything and are independently valuable: a tenant
learns what a cap would have done. Phase 4 onward is the part people pay for.

## Open questions

1. Is the cap on estimated or reconciled cost? Reconciled arrives too late to gate a live call, so it
   has to be estimated, and it will disagree with the invoice. Say so up front.
2. What happens at window rollover mid-incident? A cap that resets at midnight while a runaway agent
   is looping will let it run again.
3. Does a refused call count as an error to the customer's application, or is there a structured
   response the SDK returns? `CostLimitExceededException` is the current answer for Python; the proxy
   needs an HTTP equivalent, and picking the wrong status code will break retry logic in client
   libraries.
4. Per-feature caps summing above the tenant cap: allowed with the tenant cap winning, or rejected at
   config time?
5. Who can enable enforcement? This is the first setting that can break production, so it deserves a
   distinct permission once the hosted role model exists.

## Risks

**A guardrail that breaks production is worse than no guardrail.** Fail-open, shadow by default, and
graduated actions are the mitigations, and none is optional.

**The counter will be approximate and someone will file a bug.** Cost is known after the call, so
caps overshoot under concurrency. Document the bound rather than implying precision.

**"Budget cap" overpromises if it only counts LLM spend.** Roughly half the bill arrives from daily
connector pulls and cannot gate a live call. Naming the feature accurately is the fix.

**Downgrade changes what the end user receives.** Shipping it without disclosure on the span and to
the caller would undercut the product's own posture on honesty.
