// SPDX-License-Identifier: Apache-2.0
// Scoped budgets and per-scope forecasts (CTO-211, F7). Pure functions, no I/O, no clock.
//
// This is the last ticket of the spend-forecasting epic and it computes NO forecast of its own.
// CTO-206 owns the projection, CTO-210 owns the burn-down view model, CTO-207 owns the settled
// series and already takes a scope argument. What was missing was the layer above all three: a
// tenant with five budgets has five forecasts, and somebody has to decide which of them the page
// shows, how they line up against each other, and which ones are allowed to be added together.
//
// THE CORRECTNESS QUESTION THIS FILE EXISTS TO ANSWER
//
// Per-scope budgets can sum to more or less than the tenant-wide budget, and both are legitimate.
// The spend and the budgets behave completely differently and conflating them is the failure this
// epic has been guarding against, so they are modelled separately and rendered differently:
//
//   SPEND MUST RECONCILE. It is one bill, sliced. A slice cannot exceed the whole, and two slices
//   of the same KIND cannot overlap, because a span carries exactly one feature tag, one model and
//   one layer. So within a kind the scoped totals are summed, checked against the tenant total, and
//   the leftover is reported as an explicit residual row rather than left for a reader to compute.
//   A slice above the tenant total, or a kind summing above it, is a BUG in this page and it is
//   labelled as one (`spendWarnings`), never quietly rendered.
//
//   SPEND IS NEVER SUMMED ACROSS KINDS. A feature budget and a model budget describe the same
//   dollars from two directions: `research-agent` spend and `gpt-4o` spend overlap by construction.
//   Adding them is double counting, so `groups` is keyed by kind and there is no cross-kind total
//   anywhere in this module. When a tenant budgets across more than one kind, `mixesKinds` says so
//   and the card prints the caveat instead of a number nobody can interpret.
//
//   BUDGETS NEED NOT RECONCILE, AND OVER-ALLOCATION IS NOT A WARNING. Three feature owners each
//   given $30k against a $70k tenant budget is a deliberate, common and correct arrangement: not
//   every team spends its allocation, and finance over-allocates on purpose the same way an airline
//   overbooks. Flagging it red would train readers to ignore red on the one page where red has to
//   mean something. So `budgetAllocation` is INFORMATIONAL: it states the sum, the tenant budget and
//   the headroom or over-allocation, in neutral words, and the only thing it actually asserts is
//   arithmetic. The genuinely alarming case is not "budgets sum high", it is "the FORECASTS sum
//   above the tenant budget", which is a statement about spend, and that is what `standing` on each
//   line and the tenant line's own breach date already say.
//
// PER-SCOPE HISTORY IS THE OTHER HALF. CTO-210 found that `querySettledCostSeries` treats a day with
// no rows as a genuine zero, which is right for a measured card and wrong as forecast history: a
// six-day-old tenant arrived with 29 "settled" days, 23 of them meaning "did not exist yet". That
// bites harder per scope, because a feature introduced last week has almost no history even on a
// mature tenant. The fix is not repeated here: every line is built from `burndownSection` over that
// scope's OWN series, so the leading-zero trim and the fourteen-day floor apply per scope for free.
// What this module adds is that `standing` keeps `unknown` apart from `on_track`, so a scope that
// could not be projected can never be listed among the ones that are fine.
//
// Money is integer micro-USD. Dates are `YYYY-MM-DD` UTC strings, all of them ClickHouse's.

import { selectBudget, COMPARED_PERIOD, type TenantBudget } from "./budgetVsActual";
import type { BurndownSection } from "./burndown";
import type { ForecastStatus } from "./forecast";
import {
  isTenantScope,
  sameScope,
  scopeKey,
  scopeLabel,
  scopeOfBudget,
  SCOPE_KINDS,
  TENANT_SCOPE,
  type ForecastScope,
  type ScopeKind,
} from "./spendScopes";
import type { MicroUSD } from "./types";

/**
 * Where one scope stands. Five values, and the two that look alike are the point of the type.
 *
 * `on_track`:         projected, and the projection stays under this scope's own budget.
 * `projected_breach`: projected, and it crosses. `breachDate` says when.
 * `already_over`:     settled spend to date is ALREADY above the budget. Not a forecast at all, and
 *                     a strictly worse thing to be told than "you will cross on the 22nd".
 * `no_budget`:        this scope has no budget. Its forecast still renders; its variance does not.
 * `unknown`:          too little history for THIS scope. We are not saying it is fine. Keeping this
 *                     apart from `on_track` is the whole reason this is an enum and not a boolean.
 */
export type ScopeStanding =
  | "on_track"
  | "projected_breach"
  | "already_over"
  | "no_budget"
  | "unknown";

/** One row of the roster: a scope, its budget, its forecast, and where that leaves it. */
export interface ScopeLine {
  scope: ForecastScope;
  /** `scopeKey(scope)`. The selector's URL value and the row key. */
  key: string;
  /** `scopeLabel(scope)`. "Whole tenant", or "feature: research-agent". */
  label: string;
  /** True for the scope the page is currently showing in full. */
  selected: boolean;

  /** This scope's own monthly budget, or null when it has none. Never an implicit zero. */
  budgetMicroUsd: MicroUSD | null;
  /** Non-null exactly when `budgetMicroUsd` is null. A real sentence, for `<Blank reason>`. */
  noBudgetReason: string | null;

  /** Settled month-to-date spend for this scope. Measured, never projected. */
  settledMicroUsd: MicroUSD;
  /** Projected month-end spend for this scope, or null below its own history floor. */
  projectedMicroUsd: MicroUSD | null;
  /** Projected minus budget. Positive is over. Null without both a budget and a projection. */
  varianceMicroUsd: MicroUSD | null;
  /** As a fraction of budget. Null without both, and null when the budget is zero. */
  variancePct: number | null;

  status: ForecastStatus;
  standing: ScopeStanding;
  /** Why `standing` is what it is, in words, so a blank or a badge is never unexplained. */
  standingReason: string;
  /** `YYYY-MM-DD` the projection crosses this scope's budget, or null. */
  breachDate: string | null;

  /** Settled days of history for THIS scope, after the leading-zero trim. */
  historyDays: number;
  /** The floor `historyDays` is compared against, echoed so no UI hard-codes fourteen. */
  requiredDays: number;
  /** Leading days dropped because this scope had not been seen spending yet (CTO-210 note 3). */
  trimmedLeadingDays: number;
  /** First day this scope was seen spending anything, or null when it never was. */
  firstObservedDay: string | null;

  /** This scope's settled spend as a share of the tenant's, or null when the tenant total is zero. */
  shareOfTenantSettled: number | null;
  /** True when this scope's settled spend exceeds the tenant's, which is arithmetically impossible. */
  exceedsTenantSettled: boolean;
}

/** The scoped lines of one kind, which is the only grouping in which summing is legitimate. */
export interface ScopeKindGroup {
  kind: ScopeKind;
  keys: string[];
  /** Sum of settled month-to-date across this kind's budgeted scopes. Disjoint, so this is real. */
  settledMicroUsd: MicroUSD;
  /**
   * Tenant settled minus the above: spend of this month that no budgeted scope of this kind covers.
   * Shown as its own row, because the alternative is a reader silently assuming the rows are the
   * whole bill. Never negative unless something is wrong, and `spendReconciles` catches that.
   */
  residualMicroUsd: MicroUSD;
  /** Sum of the budgets of this kind. Informational: budgets need not reconcile. */
  budgetMicroUsd: MicroUSD;
  /** How many of this kind's scopes actually carry a budget. */
  budgetedCount: number;
}

/**
 * Budgets against budgets. INFORMATIONAL BY DESIGN, see the header: over-allocation is a normal
 * management posture and this structure deliberately carries no severity.
 */
export interface BudgetAllocation {
  /** The tenant-wide monthly budget, or null when none is set. */
  tenantBudgetMicroUsd: MicroUSD | null;
  /** Sum of the scoped budgets, per kind. Never summed across kinds: that would double count. */
  perKind: { kind: ScopeKind; budgetMicroUsd: MicroUSD; count: number }[];
  /**
   * True when some kind's budgets sum ABOVE the tenant budget. A neutral fact, not a warning: teams
   * over-allocate on purpose. Null when there is no tenant budget to compare against.
   */
  overAllocated: boolean | null;
  /** A sentence stating the arithmetic, or null when there is nothing to state. */
  note: string | null;
}

export interface ScopeReconciliation {
  /** Settled month-to-date for the whole tenant. The denominator of every share on this page. */
  tenantSettledMicroUsd: MicroUSD;
  groups: ScopeKindGroup[];
  /** True when more than one kind is budgeted, so no roster-wide sum of spend is meaningful. */
  mixesKinds: boolean;
  /**
   * True when the spend reconciles: no scope above the tenant, no kind summing above it. Spend, not
   * budgets. False is a bug in this page, and the card says so on screen.
   */
  spendReconciles: boolean;
  /** One sentence per violation, empty when `spendReconciles`. */
  spendWarnings: string[];
  budgetAllocation: BudgetAllocation;
}

export interface ScopedForecast {
  /** The scope shown in full. Tenant-wide unless the reader chose otherwise. */
  selected: ForecastScope;
  /** Non-null when the requested scope could not be honoured, saying what was shown instead. */
  selectionFallbackReason: string | null;
  /** The full burn-down section for `selected`: chart, cone, breach date, layer split. */
  section: BurndownSection;
  /** Every scope on the roster, tenant-wide first, then by settled spend descending. */
  lines: ScopeLine[];
  reconciliation: ScopeReconciliation;
}

/** One scope's already-computed section, as the route hands them in. */
export interface ScopeSection {
  scope: ForecastScope;
  section: BurndownSection;
}

function ratio(part: number, whole: number): number | null {
  // Zero denominator is undefined, not infinite and not zero. Callers render a blank.
  return whole === 0 ? null : part / whole;
}

/**
 * Which scopes the page should offer, given this tenant's budgets.
 *
 * Tenant-wide always, first, whether or not it has a budget: it is the default view and the
 * denominator everything else is checked against. Then every scope carrying a MONTHLY budget that
 * covers today, deduplicated and ordered by kind then value so the selector does not reshuffle
 * between renders.
 *
 * Deliberately NOT every feature the tenant has ever emitted. A tenant with 200 feature tags would
 * get a 200-entry selector and 200 ClickHouse reads, and 195 of those scopes have no budget and
 * therefore nothing to be on track against. A budget is the statement that somebody owns this slice.
 */
export function rosterScopes(
  budgets: readonly TenantBudget[],
  today: string,
): ForecastScope[] {
  const byKey = new Map<string, ForecastScope>();
  byKey.set(scopeKey(TENANT_SCOPE), TENANT_SCOPE);
  for (const b of budgets) {
    // Same filter as `selectBudget`: a quarterly budget is not offered on a month-scoped page,
    // because comparing a quarter's dollars against a month of spend reports everyone as fine.
    if (b.period !== COMPARED_PERIOD) continue;
    if (b.starts_on > today) continue;
    if (b.ends_on !== null && b.ends_on < today) continue;
    const scope = scopeOfBudget(b);
    if (!scope) continue;
    byKey.set(scopeKey(scope), scope);
  }
  const all = [...byKey.values()];
  return all.sort((a, b) => {
    if (a.kind !== b.kind) return SCOPE_KINDS.indexOf(a.kind) - SCOPE_KINDS.indexOf(b.kind);
    return a.value.localeCompare(b.value);
  });
}

function standingOf(
  section: BurndownSection,
  budgetMicroUsd: MicroUSD | null,
  label: string,
): { standing: ScopeStanding; reason: string } {
  const { forecast, settledPeriodMicroUsd } = section;
  if (forecast.status === "insufficient_history") {
    return {
      standing: "unknown",
      reason:
        `${label} has ${section.window.dayCount} settled ${section.window.dayCount === 1 ? "day" : "days"} ` +
        `of its own history and ${section.window.requiredDays} are needed, so nothing was projected. ` +
        "This is not a statement that it is on track.",
    };
  }
  if (budgetMicroUsd === null) {
    return {
      standing: "no_budget",
      reason: section.noBudgetReason ?? `no monthly budget is set for ${label}`,
    };
  }
  // Already over is checked before the projection: it is a measured fact and it outranks a forecast.
  if (settledPeriodMicroUsd > budgetMicroUsd) {
    return {
      standing: "already_over",
      reason: `${label} has already spent more than its monthly budget; this is measured, not projected`,
    };
  }
  const projected = section.forecast.projectedMicroUsd;
  if (projected !== null && projected > budgetMicroUsd) {
    return {
      standing: "projected_breach",
      reason:
        section.forecast.breach.date !== null
          ? `${label} is projected to cross its budget on ${section.forecast.breach.date}`
          : `${label} is projected to end the month over its budget`,
    };
  }
  return {
    standing: "on_track",
    reason: `${label} is projected to end the month within its budget`,
  };
}

function lineFrom(
  entry: ScopeSection,
  selected: ForecastScope,
  tenantSettledMicroUsd: MicroUSD,
): ScopeLine {
  const { scope, section } = entry;
  const label = scopeLabel(scope);
  const budgetMicroUsd = section.budget ? section.budget.amountMicroUsd : null;
  const { standing, reason } = standingOf(section, budgetMicroUsd, label);
  const settledMicroUsd = section.settledPeriodMicroUsd;
  return {
    scope,
    key: scopeKey(scope),
    label,
    selected: sameScope(scope, selected),
    budgetMicroUsd,
    noBudgetReason: budgetMicroUsd === null ? section.noBudgetReason : null,
    settledMicroUsd,
    projectedMicroUsd: section.forecast.projectedMicroUsd,
    varianceMicroUsd: section.varianceMicroUsd,
    variancePct: section.variancePct,
    status: section.forecast.status,
    standing,
    standingReason: reason,
    breachDate: section.forecast.breach.outcome === "breaches" ? section.forecast.breach.date : null,
    historyDays: section.window.dayCount,
    requiredDays: section.window.requiredDays,
    trimmedLeadingDays: section.window.trimmedLeadingDays,
    firstObservedDay: section.window.firstObservedDay,
    // The tenant's own share is 100 percent by definition; it is still computed the same way so the
    // column has no special case in it.
    shareOfTenantSettled: ratio(settledMicroUsd, tenantSettledMicroUsd),
    exceedsTenantSettled: !isTenantScope(scope) && settledMicroUsd > tenantSettledMicroUsd,
  };
}

/**
 * Assemble the roster, the selection and the reconciliation from per-scope sections.
 *
 * `sections` must contain the tenant-wide scope: it is the denominator of every share and the total
 * every group is checked against, and there is no honest version of this page without it. Each
 * section must have been built from ITS OWN `querySettledCostSeries(scope)` result, not from a
 * filtered tenant series, or the per-scope history guard is not actually per scope.
 */
export function scopedForecast(input: {
  sections: readonly ScopeSection[];
  /** What the reader asked for. Falls back to tenant-wide, saying so, when it is not on the roster. */
  requested: ForecastScope | null;
  /** Why the request could not be honoured, when the caller already knows (an unparsable `?scope=`). */
  requestedReason?: string | null;
  budgets: readonly TenantBudget[];
}): ScopedForecast {
  const tenantEntry = input.sections.find((s) => isTenantScope(s.scope));
  if (!tenantEntry) {
    throw new Error("scopedForecast: the tenant-wide section is required and was not supplied");
  }

  const requested = input.requested;
  const match = requested ? input.sections.find((s) => sameScope(s.scope, requested)) : null;
  const chosen = match ?? tenantEntry;
  const selectionFallbackReason =
    match || (!requested && !input.requestedReason)
      ? null
      : requested
        ? `${scopeLabel(requested)} has no monthly budget covering today, so the tenant-wide ` +
          "forecast is shown instead"
        : (input.requestedReason ??
          "the requested scope could not be read, so the tenant-wide forecast is shown instead");

  const tenantSettled = tenantEntry.section.settledPeriodMicroUsd;
  const lines = input.sections.map((s) => lineFrom(s, chosen.scope, tenantSettled));

  // Tenant-wide first because it is the total, then by settled spend so a feature owner scanning for
  // their own line finds the biggest ones where they expect them.
  lines.sort((a, b) => {
    if (a.scope.kind === "tenant") return -1;
    if (b.scope.kind === "tenant") return 1;
    return b.settledMicroUsd - a.settledMicroUsd || a.label.localeCompare(b.label);
  });

  const scoped = lines.filter((l) => l.scope.kind !== "tenant");
  const kinds = SCOPE_KINDS.filter(
    (k) => k !== "tenant" && scoped.some((l) => l.scope.kind === k),
  );
  const groups: ScopeKindGroup[] = kinds.map((kind) => {
    const members = scoped.filter((l) => l.scope.kind === kind);
    // Legitimate ONLY within a kind: a span carries one feature tag, one model and one layer, so
    // two scopes of the same kind cannot both claim the same dollar.
    const settledMicroUsd = members.reduce((sum, l) => sum + l.settledMicroUsd, 0);
    const budgeted = members.filter((l) => l.budgetMicroUsd !== null);
    return {
      kind,
      keys: members.map((l) => l.key),
      settledMicroUsd,
      residualMicroUsd: tenantSettled - settledMicroUsd,
      budgetMicroUsd: budgeted.reduce((sum, l) => sum + (l.budgetMicroUsd ?? 0), 0),
      budgetedCount: budgeted.length,
    };
  });

  const spendWarnings: string[] = [];
  for (const l of scoped) {
    if (l.exceedsTenantSettled) {
      spendWarnings.push(
        `${l.label} shows more settled spend this month than the whole tenant does. A slice cannot ` +
          "exceed the bill, so that is a bug on this page rather than a fact about your spend.",
      );
    }
  }
  for (const g of groups) {
    if (g.residualMicroUsd < 0) {
      spendWarnings.push(
        `The budgeted ${g.kind} scopes sum to more settled spend this month than the whole tenant ` +
          "does. Scopes of one kind are disjoint, so they cannot double count; that gap is a bug on " +
          "this page.",
      );
    }
  }

  const tenantBudgetRow = selectBudget(
    input.budgets,
    "tenant",
    "",
    tenantEntry.section.period.today,
  );
  const tenantBudgetMicroUsd = tenantBudgetRow ? tenantBudgetRow.amount_micro : null;
  const perKind = groups
    .filter((g) => g.budgetedCount > 0)
    .map((g) => ({ kind: g.kind, budgetMicroUsd: g.budgetMicroUsd, count: g.budgetedCount }));
  const overAllocated =
    tenantBudgetMicroUsd === null || perKind.length === 0
      ? null
      : perKind.some((k) => k.budgetMicroUsd > tenantBudgetMicroUsd);

  return {
    selected: chosen.scope,
    selectionFallbackReason,
    section: chosen.section,
    lines,
    reconciliation: {
      tenantSettledMicroUsd: tenantSettled,
      groups,
      mixesKinds: kinds.length > 1,
      spendReconciles: spendWarnings.length === 0,
      spendWarnings,
      budgetAllocation: {
        tenantBudgetMicroUsd,
        perKind,
        overAllocated,
        note: allocationNote(tenantBudgetMicroUsd, perKind, overAllocated),
      },
    },
  };
}

/**
 * The one sentence the allocation summary is allowed to say.
 *
 * Neutral on purpose, per the header. It states the arithmetic and names over-allocation as a
 * deliberate posture rather than a problem, because the alternative trains readers to ignore the
 * page's actual warnings.
 */
function allocationNote(
  tenantBudgetMicroUsd: MicroUSD | null,
  perKind: { kind: ScopeKind; budgetMicroUsd: MicroUSD; count: number }[],
  overAllocated: boolean | null,
): string | null {
  if (perKind.length === 0) return null;
  if (tenantBudgetMicroUsd === null) {
    return (
      "Scoped budgets are set but no tenant-wide budget is, so there is nothing to allocate " +
      "against. That is a normal state: a scoped budget stands on its own."
    );
  }
  if (overAllocated) {
    return (
      "The scoped budgets sum to more than the tenant-wide budget. That is allowed and often " +
      "deliberate: an owner is given a ceiling they are not expected to reach, the same way a " +
      "flight is overbooked. Budgets are statements of intent per owner and need not add up. The " +
      "SPEND below does add up, and that is what is checked."
    );
  }
  return (
    "The scoped budgets sum to less than the tenant-wide budget, leaving headroom that no scope " +
    "owns. Also normal: budgets need not add up. The spend below does, and that is what is checked."
  );
}

/** What the scoped section carries over the wire. Exactly one of the two fields is non-null. */
export interface ScopedForecastPayload {
  scoped: ScopedForecast | null;
  /** Why there is no scoped forecast: ClickHouse or the gateway could not be read. Never "no budget". */
  unavailable: string | null;
}
