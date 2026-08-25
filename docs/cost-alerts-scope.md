# Scope: cost alerts with Slack delivery

**Status: proposal, not built.** Alert a tenant when AI spend crosses a threshold per day, week or
month, and deliver it to Slack.

## What exists, and what does not

A `cost_cap` guardrail kind already exists (`db/postgres/0006_tenant_guardrails.sql`), but it solves
a different problem. Guardrails are **preventive**: the SDK engine enforces them in-flight and can
alter agent behavior (`observe` / `warn` / `graceful` / `hard_stop`). An alert is **detective**: it
watches aggregate spend after the fact and tells a human. A cap stops one expensive run. An alert
tells you the bill tripled last Tuesday. Both are wanted, and neither substitutes for the other.

What does not exist is more important:

**There is no outbound notification of any kind, anywhere in the product.** No Slack, no webhook, no
email. Every integration today is inbound (Stripe webhook, HubSpot, Segment, Pendo pull). This
feature introduces the first path where ai-tally initiates contact with the outside world, which
brings retry semantics, delivery failure handling, and secret storage for a destination we push to.

**Nothing is scheduled.** As with the cost connectors, the repo has workers but no scheduler calling
them on a timer. An alert evaluator is useless without one, so this feature either builds the
scheduler or inherits it from whatever lands first. This is now the third feature blocked on the
same missing piece, which argues for building it properly once.

## Decision 1: what the threshold means

"Cost goes up beyond X per day" has two readings and they need different machinery.

**Absolute.** "Tell me when a day costs more than $500." Easy to build, easy to explain, and it
either fires constantly or never as the business grows. Static thresholds go stale.

**Relative.** "Tell me when today is 40 percent above the trailing 7-day median." Catches the thing
people actually care about (a regression, a runaway loop, a bad deploy) and does not need retuning
as the business scales.

Recommendation: support both, ship absolute first. Absolute is what people ask for and it is
honest and obvious. Relative is what keeps the feature useful in month six. A relative rule needs a
minimum history (at least 14 days) before it can fire, and should say so rather than firing on a
thin baseline.

Explicitly out of scope for v1: statistical anomaly detection. Seasonality in AI spend is real
(weekday and weekend differ sharply) and a naive model will cry wolf. A median-based relative rule
gets most of the value without pretending to be a forecaster.

## Decision 2: the late-data problem

This is the one that will make or break the feature's credibility, and it is easy to miss.

Cost data is not complete when the day ends:

- Connector-sourced compute and egress spend arrives from a **daily** cloud billing pull, and cloud
  bills themselves lag by hours.
- Reconciled cost replaces estimated cost later, through `reconciliation_runs`.
- Telemetry can be sampled, so totals shift as sampling is accounted for.

An evaluator that checks "yesterday's spend" at 00:05 will see the LLM half of the bill and none of
the infra half, then see the rest arrive later. On the current demo tenant the infra layers are
about 47 percent of spend, so the same day could read $50k at midnight and $95k by noon. Alerting on
the first number produces both false negatives and, once the rest lands, a confusing second alert.

Options: wait a fixed grace period before evaluating a closed period (simple, recommended, roughly
6 to 24 hours configurable); or evaluate continuously and mark alerts provisional until the period
is settled (more responsive, much more explaining to do in Slack).

Recommendation: grace period for v1, and the alert message states the window it evaluated and
whether reconciled cost was included. An alert that cannot say what it measured is not actionable.

## Decision 3: Slack delivery

Two ways in.

**Incoming webhook URL.** The tenant creates a webhook in Slack and pastes the URL. Simple, no OAuth,
no app review. The URL is itself a secret and must be stored by reference like every other credential
in this codebase (see `gateway.connectors.config_admin`), never raw.

**Slack app with OAuth.** Nicer (channel picker, richer formatting, threading), and it means building
and maintaining a distributed Slack app, an OAuth callback, and token refresh.

Recommendation: webhook for v1. It is a fraction of the work and covers the ask. Design the
notification layer behind a transport interface so a Slack app, email, PagerDuty or a generic webhook
can be added without touching the evaluator. Given this is the product's first outbound path, the
seam matters more than the first implementation.

## Data model

```
tenant_cost_alerts
  tenant_id        uuid      references tenants(id)
  alert_id         text
  period           text      check (period in ('day','week','month'))
  kind             text      check (kind in ('absolute','relative'))
  threshold_micro  bigint    -- absolute: the cost ceiling
  threshold_pct    numeric   -- relative: percent above trailing median
  scope_kind       text      check (scope_kind in ('tenant','feature','model','layer'))
  scope_value      text      -- '' for tenant-wide
  destination_ref  text      -- secret reference to the Slack webhook, never the raw URL
  channel_hint     text      -- display only, e.g. '#ai-costs'
  state            text      check (state in ('enabled','shadow','disabled'))
  cooldown_minutes int       default 1440
  last_fired_at    timestamptz
  primary key (tenant_id, alert_id)

tenant_cost_alert_events        -- what fired, when, what it measured, delivery outcome
  tenant_id, alert_id, fired_at, period_start, period_end,
  observed_micro, threshold_micro, baseline_micro,
  delivery_status text check (delivery_status in ('sent','failed','suppressed')),
  delivery_error text
```

The `shadow` state is worth keeping for the same reason guardrails have it: a tenant can watch what
an alert *would* have fired for a week before letting it page anyone. That graduation ladder is
already the product's posture and alerts should inherit it.

`tenant_cost_alert_events` is not just a log. It powers cooldown, dedupe, and an "alert history"
view, and it is how you answer "why did this fire?" a month later.

## Suppression

An alert that fires every hour for the same overspend is noise, and noise gets muted, which means
the next real alert is missed. Three mechanisms, all needed:

- **Cooldown.** Do not re-fire the same alert within `cooldown_minutes`. Default 24 hours for a daily
  alert.
- **One alert per closed period.** A daily alert fires at most once for a given day.
- **Recovery.** When spend returns below threshold, post a short resolution message rather than
  leaving the channel with an unresolved alarm.

## The evaluator

A worker that, per tenant, per enabled alert: resolves the period window (respecting the grace
period), queries spend for the scope, compares against threshold or trailing baseline, checks
cooldown, and delivers. Delivery outcome is written to `tenant_cost_alert_events` whether it
succeeded or not.

Cost queries reuse the existing shapes in `web/lib/clickhouse.ts` (the layer case, the tenant and
window filters), so scoping an alert to a feature or a layer is a `WHERE` clause, not new machinery.

Failure posture matches the rest of the product: if the evaluator cannot compute a number it records
a failed run and stays quiet. It never sends a guessed figure, and it never silently skips. A tenant
should be able to see that the evaluator ran and found nothing, which is different from not running.

## UI

Extend `/guardrails`, or a new `/alerts` tab. Recommendation is a new tab: guardrails are about
changing agent behavior and alerts are about telling humans, and merging them muddles a page that
currently has one clear story.

Per alert: period, scope, threshold, destination, state, last fired, and the recent history from the
events table. A "test alert" button that posts to Slack immediately is worth building, because the
first thing anyone does after configuring a webhook is wonder whether it works.

## Phasing

1. **Notification transport plus Slack webhook**, with a test-send. Standalone and independently
   useful.
2. **Absolute thresholds, tenant-wide, daily**, with the scheduler and the grace period.
3. **Weekly and monthly periods, plus scoping** to feature, model or layer.
4. **Relative thresholds** against a trailing median, with a minimum-history guard.
5. **Shadow mode and alert history** in the UI.

## Open questions

1. Who owns the scheduler? Three features now need it (connectors, alerts, and any waste scan). It
   should be built once, deliberately, rather than three times.
2. Does an alert evaluate estimated cost, reconciled cost, or both? They differ, and the answer
   changes when an alert can fire.
3. Per-tenant or per-user destinations? A tenant with several teams may want the research agent's
   alerts in one channel and support's in another. The schema above allows it per alert; the UI
   needs to make it obvious.
4. What happens when Slack delivery fails? Retry with backoff, surface in the UI, both?
5. Is there a global rate limit on alerts per tenant per hour, to protect against a misconfigured
   rule spamming a channel?

## Risks

**Alert fatigue is the failure mode.** A cost alerting feature that fires too often gets muted in
week two and provides negative value from then on. Suppression and shadow mode are not polish here,
they are the feature.

**First outbound path.** Pushing to an external service introduces a class of problem the product has
not had: partial delivery, retry storms, and a stored secret that lets us post into a customer's
Slack. Treat the destination reference with the same care as any other credential.

**A wrong alert is worse than no alert.** If the late-data problem is not handled, the first alert a
customer receives will be wrong, and they will not trust the second one.
