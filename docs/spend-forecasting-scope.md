# Scope: spend forecasting and burn-down

**Status: proposal, not built.** Projected month-end spend against a budget, with a burn-down chart.

## What exists

A projection engine exists but answers a different question. `sdk/python/src/tally/projection.py`
and `/estimate` do **pre-deploy** estimation: "if I ship this prompt or model change, what happens to
cost per run?" It projects p99 cost per run and a blow-up probability over a replayed sample. That is
change-impact forecasting, not calendar forecasting.

Its posture is worth stealing: it reports p99 rather than the mean, on the grounds that a change can
leave the average flat while fattening the tail. The same instinct applies here.

**There is no budget concept for a tenant.** The only budgets in the schema are
`tenant_replay_config.daily_budget_usd` and `tenant_eval_config.daily_budget_usd`, which cap what
ai-tally itself spends running replays and evals. Nothing records what a customer intends to spend
on AI. That has to be built before "versus budget" means anything.

The inputs for the forecast half are all present: `daily_feature_rollup`, the cost series queries in
`web/lib/clickhouse.ts`, and 30 days of history per tenant.

## Decision 1: the projection method

Four options, and the differences matter most early in the month.

**Naive run-rate.** `spend_so_far / days_elapsed * days_in_month`. One line of code, and badly wrong
on day 2, and wrong every weekend. Fine as a floor, not as the headline.

**Day-of-week weighted.** AI spend has strong weekday and weekend structure: a B2B product can
easily run at a third of weekday volume on a Sunday. Weighting the remaining days by their day-of-week
profile from trailing history fixes the largest systematic error for very little complexity.

**Trailing median run-rate.** Project the remaining days from a trailing 7 or 14 day median rather
than the month-to-date mean. Robust to a single spike distorting the whole projection.

**Time series model.** Holt-Winters or similar. More machinery than the data supports at 30 days of
history, and it will look authoritative while being wrong.

Recommendation: day-of-week weighted median for the point estimate, and show the naive run-rate
alongside it as a sanity line. Explicitly out of scope: anything that needs more than a few months
of history, since most tenants will not have it.

## Decision 2: say how uncertain it is

A single projected number invites a precision it does not have. Two honest options:

- **A range.** Project a low and high band from the variance of trailing daily spend. "Between $78k
  and $96k, most likely $86k."
- **A confidence that degrades with time.** On day 2 of the month the projection is nearly
  meaningless; on day 25 it is nearly certain. The UI should visibly reflect that, for example by
  widening the cone early and narrowing it as the month closes.

Recommendation: both. A cone that narrows through the month is the clearest possible statement of
"this gets more trustworthy as we go", and it stops anyone treating a day-3 number as a commitment.

Refuse to project at all below a minimum history, the same way `/compare` renders a dash rather than
a quality score below 10 judged samples. A forecast from four days of data should say "not enough
history yet", not print a number.

## Decision 3: late data, again

The same problem the alerts scope has, and it bites harder here because a forecast compounds it.

Connector-sourced compute and egress arrive on a daily pull and lag the cloud bill. Reconciliation
replaces estimated cost with invoiced cost afterwards. On the current demo tenant, infra layers are
about 47 percent of spend, so today's partial number is roughly half the eventual total.

A run-rate computed over a window whose last day is half-reported will systematically **underestimate**
month-end. The fix is to exclude unsettled days from the baseline and to say which days the
projection was computed from. A forecast that cannot state its input window is not auditable.

## The budget model

```
tenant_budgets
  tenant_id      uuid references tenants(id)
  budget_id      text
  period         text    check (period in ('month','quarter'))
  amount_micro   bigint
  scope_kind     text    check (scope_kind in ('tenant','feature','model','layer'))
  scope_value    text    -- '' for tenant-wide
  starts_on      date
  ends_on        date
  primary key (tenant_id, budget_id)
```

Scoped budgets matter more than they first appear. "The research agent gets $30k a month" is how
teams actually govern spend, and it makes the burn-down useful to a feature owner rather than only
to finance.

## The page

Either a section on `/cost` or its own `/forecast` tab. Recommendation is a section on `/cost` first:
it is the same question a user already came to that page with, and a separate tab for one chart is
premature.

**Headline.** Projected month-end spend, the range, month-to-date actual, budget, and projected
variance in both dollars and percent. The variance sign is the whole point, so it should be the most
legible thing on screen.

**Burn-down chart.** Cumulative spend to date as a solid line, the projection as a widening cone, and
budget as a flat reference line. Where the cone crosses the budget line is the forecast breach date,
and that date is the single most useful output of the feature. "You cross budget on the 22nd" beats
any percentage.

**Layer split.** Because hidden cost is the product's core story, the forecast should break down by
layer, not just total. "You will land 12 percent over, and compute is the reason" is actionable in a
way a single number is not.

Honest-under-uncertainty throughout: no budget configured renders the forecast without a variance
rather than assuming a budget of zero, and insufficient history renders a prompt instead of a number.

## Relationship to alerts

This shares most of its machinery with `docs/cost-alerts-scope.md`, and the two should be designed
together rather than sequentially:

- A budget breach is the most valuable alert this product can send, and it is a *forecast* alert:
  "at current run-rate you will exceed budget on the 22nd" arrives days before "you exceeded budget",
  which arrives too late to act on.
- Both need the same period windows, the same late-data grace period, and the same scoping.
- Both need the scheduler that does not exist yet.

If only one ships, the forecast is the more valuable half, because a forecast alert is strictly
better than a threshold alert for budget governance.

## Phasing

1. **The budget model** plus a settings UI to enter one.
2. **Month-to-date actual versus budget**, no projection. Useful immediately and nearly free.
3. **The projection**: day-of-week weighted, with the minimum-history guard and the unsettled-day
   exclusion.
4. **The burn-down chart** with the cone and the breach date.
5. **Layer and feature-scoped budgets and forecasts.**
6. **Forecast breach alerts**, once the alerting transport exists.

## Open questions

1. Calendar month or the tenant's billing month? These differ for most cloud contracts, and a
   forecast against the wrong period is useless to finance.
2. Estimated cost, reconciled cost, or both? A forecast on estimates and a budget tracked on invoices
   will not reconcile, and someone will notice.
3. Does the budget roll over, and is an annual budget divided evenly or seasonally?
4. Who sets budgets, and is that a permission distinct from viewing them? Relevant to the hosted
   scope's role model.
5. Should the forecast account for known future changes, for example a model migration already
   scheduled? That is where this feature meets `/estimate`, and it is a natural but large extension.

## Risks

**A wrong forecast early in the month.** Day-2 projections are volatile, and a customer who sees
"projected $340k" against a $90k budget will lose confidence permanently. The minimum-history guard
and the widening cone are the mitigations, and neither is optional.

**Budget becomes a compliance object.** Once finance sees a budget field, it becomes a number people
are measured against, and the accuracy bar rises accordingly. Worth being clear that this is a
forecast and not a commitment.

**Double-counting with reconciliation.** If the projection mixes estimated and reconciled cost
carelessly, month-end actual will not match the last projection, and the feature will look broken
even when it is right.
