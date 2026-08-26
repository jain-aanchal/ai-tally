// SPDX-License-Identifier: Apache-2.0
// The scope vocabulary shared by the scoped forecast (CTO-211, F7). Pure, no I/O, no clock.
//
// CTO-205 gave `tenant_budgets` a `(scope_kind, scope_value)` pair and CTO-207 gave
// `querySettledCostSeries` a matching `SpendScope` argument. Both already speak the same four kinds.
// What did not exist was one place that turns that pair into the three things a UI needs: a stable
// key for a URL, a label a human reads, and the parse back. Writing those inline in the route and
// again in the card is how the selector and the API end up disagreeing about what
// `feature:research-agent` means.
//
// WHY THIS IS ITS OWN MODULE and not part of `burndown.ts` or `scopedForecast.ts`: `burndown.ts`
// needs the type to select the right budget, `scopedForecast.ts` needs the whole vocabulary, and the
// two already depend on each other in one direction. A shared leaf module keeps that acyclic and
// keeps this file importable from a client component (nothing here touches the server).
//
// The key format is `kind` for tenant-wide and `kind:value` otherwise. Only the FIRST colon splits,
// because a model id legitimately contains colons (`bedrock:anthropic.claude-3`) and splitting on
// all of them would silently truncate the scope to a prefix, i.e. select a different slice of spend
// than the one named in the URL.

/** The four scope kinds CTO-205 stores and CTO-207 can query. Kept in this order for the selector. */
export const SCOPE_KINDS = ["tenant", "feature", "model", "layer"] as const;
export type ScopeKind = (typeof SCOPE_KINDS)[number];

/**
 * One slice of spend to forecast.
 *
 * `value` is `''` exactly when `kind` is `tenant`, mirroring the storage: CTO-205 writes `''` rather
 * than NULL for a tenant-wide budget so its exclusion constraint has something to compare.
 */
export interface ForecastScope {
  kind: ScopeKind;
  value: string;
}

/** The whole bill. The default everywhere: a reader who has not chosen a scope means "all of it". */
export const TENANT_SCOPE: ForecastScope = { kind: "tenant", value: "" };

export function isTenantScope(scope: ForecastScope): boolean {
  return scope.kind === "tenant";
}

/** Stable identity for a scope: a URL query value, a React key, a map key. */
export function scopeKey(scope: ForecastScope): string {
  return scope.kind === "tenant" ? "tenant" : `${scope.kind}:${scope.value}`;
}

/** How a scope reads in a heading or a table cell. */
export function scopeLabel(scope: ForecastScope): string {
  return scope.kind === "tenant" ? "Whole tenant" : `${scope.kind}: ${scope.value}`;
}

/** Just the noun, for sentences like "this feature has too little history". */
export function scopeNoun(scope: ForecastScope): string {
  return scope.kind === "tenant" ? "tenant" : scope.kind;
}

export function sameScope(a: ForecastScope, b: ForecastScope): boolean {
  return a.kind === b.kind && a.value === b.value;
}

/**
 * Parse a key back into a scope, or null when it names nothing this deployment can forecast.
 *
 * Null rather than a fallback to tenant-wide on purpose. A `?scope=` the server cannot parse means
 * the reader asked for something specific and got something else, and silently swapping in the
 * tenant total would put whole-tenant numbers under a heading naming one feature. The caller falls
 * back explicitly and says it did.
 */
export function parseScopeKey(key: string | null | undefined): ForecastScope | null {
  if (!key) return null;
  if (key === "tenant") return TENANT_SCOPE;
  const colon = key.indexOf(":");
  if (colon <= 0) return null;
  const kind = key.slice(0, colon);
  const value = key.slice(colon + 1);
  if (!value) return null;
  // `tenant:something` is not a scope: tenant-wide names nothing by construction.
  if (kind === "tenant") return null;
  if (!(SCOPE_KINDS as readonly string[]).includes(kind)) return null;
  return { kind: kind as ScopeKind, value };
}

/** The scope a budget row governs. Snake case in, camel case out; `scope_value` is never null. */
export function scopeOfBudget(budget: { scope_kind: string; scope_value: string }): ForecastScope | null {
  if (!(SCOPE_KINDS as readonly string[]).includes(budget.scope_kind)) return null;
  const kind = budget.scope_kind as ScopeKind;
  if (kind === "tenant") return TENANT_SCOPE;
  if (!budget.scope_value) return null;
  return { kind, value: budget.scope_value };
}
