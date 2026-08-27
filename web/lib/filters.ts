// SPDX-License-Identifier: Apache-2.0
// Shared dashboard filter state, URL-synced (CTO-221, D1 design foundation).
//
// This is the single source of truth for "what slice of spend am I looking at": a time range, a
// group-by dimension, and multi-select dimension filters. The state lives in the URL query string
// (parsed here, driven by `useFilters`) so a filtered view is shareable and the browser back button
// walks the filter history rather than the page history.
//
// Two hard constraints shape this file:
//
//   1. It PRESERVES `?tag=` and `?scope=` (CTO-104 / CTO-211) and any other unrelated query param.
//      Serialization only ever touches the keys this module owns (`MANAGED_KEYS`); everything else
//      on the incoming URLSearchParams is carried through untouched. Clobbering `tag`/`scope` would
//      silently change which feature the breakdown is filtered to or which budget the forecast
//      reports on, from an unrelated filter interaction.
//   2. It round-trips. `parseFilters(filtersToSearchParams(state, base))` yields `state` back for
//      any normalized state, which is what makes a shared link reconstruct the exact view and what
//      the D1 acceptance test asserts. Default values are OMITTED from the URL (a bare `/cost` is
//      the 30-day, group-by-layer, unfiltered view) precisely so the round-trip holds at the
//      defaults too: an empty query parses to the defaults, and the defaults serialize to empty.
//
// This module is deliberately pure (no React, no `next/navigation`) so the round-trip is testable
// without a router and so both the client hook and the server route can read the same parser.

/** Time-range presets. Custom carries an explicit `from`/`to`. 30 days is the historical default. */
export type TimeRangePreset = "7d" | "30d" | "90d" | "custom";

export const TIME_RANGE_PRESETS: readonly TimeRangePreset[] = ["7d", "30d", "90d", "custom"];

/** Days for a fixed preset. `custom` has none: its span comes from `from`/`to`. */
export const PRESET_DAYS: Record<Exclude<TimeRangePreset, "custom">, number> = {
  "7d": 7,
  "30d": 30,
  "90d": 90,
};

/**
 * The dimensions a page can group by or filter on. These are also the multi-select filter keys, so
 * the list drives both the group-by selector and the set of dimension-filter URL params.
 */
// `agent` (= ServiceName) folds the retired /agents view into the explorer as one more group-by
// dimension (CTO-241, M2 of the unified Cost explorer CTO-239). It is a first-class dimension so it
// flows through group-by, the multi-select filter, and the shareable URL exactly like the others.
export const DIMENSIONS = ["feature", "model", "layer", "provider", "account", "agent"] as const;
export type Dimension = (typeof DIMENSIONS)[number];

export const DIMENSION_LABEL: Record<Dimension, string> = {
  feature: "Feature",
  model: "Model",
  layer: "Layer",
  provider: "Provider",
  account: "Account",
  agent: "Agent",
};

export interface TimeRange {
  preset: TimeRangePreset;
  /** ISO yyyy-mm-dd, only meaningful (and only non-null) when `preset === "custom"`. */
  from: string | null;
  to: string | null;
}

/** One string array per dimension. Empty array = no filter on that dimension. */
export type DimensionFilters = Record<Dimension, string[]>;

export interface FilterState {
  range: TimeRange;
  groupBy: Dimension;
  filters: DimensionFilters;
}

export const DEFAULT_RANGE_PRESET: TimeRangePreset = "30d";
export const DEFAULT_GROUP_BY: Dimension = "layer";

/** The URL query keys this module owns. Serialization touches ONLY these; all else is preserved. */
export const MANAGED_KEYS: readonly string[] = ["range", "from", "to", "groupBy", ...DIMENSIONS];

function emptyFilters(): DimensionFilters {
  return { feature: [], model: [], layer: [], provider: [], account: [], agent: [] };
}

export function defaultFilterState(): FilterState {
  return {
    range: { preset: DEFAULT_RANGE_PRESET, from: null, to: null },
    groupBy: DEFAULT_GROUP_BY,
    filters: emptyFilters(),
  };
}

/** A plausible ISO calendar date `yyyy-mm-dd`. Rejects garbage so custom ranges never fabricate. */
export function isIsoDate(s: string | null | undefined): s is string {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  // Round-trip through Date to reject impossible days (2026-02-31): the UTC constructor normalizes,
  // so a valid date comes back byte-identical.
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** Normalize a comma-joined multi-select value: split, trim, drop blanks, dedupe, keep order. */
export function parseMulti(raw: string | null | undefined): string[] {
  if (!raw) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of raw.split(",")) {
    const v = part.trim();
    if (v && !seen.has(v)) {
      seen.add(v);
      out.push(v);
    }
  }
  return out;
}

/**
 * Parse a URL's query into a {@link FilterState}. Unknown / malformed values degrade to the default
 * rather than throwing, so a hand-edited or stale link can never blank the page: a bad `groupBy`
 * reads as the default dimension, a `custom` range missing a valid `from`/`to` falls back to the
 * 30-day preset (honest-under-uncertainty: we do not invent a window).
 */
export function parseFilters(sp: URLSearchParams): FilterState {
  const state = defaultFilterState();

  const rawRange = sp.get("range");
  if (rawRange && (TIME_RANGE_PRESETS as readonly string[]).includes(rawRange)) {
    const preset = rawRange as TimeRangePreset;
    if (preset === "custom") {
      const from = sp.get("from");
      const to = sp.get("to");
      // Both ends must be real dates and ordered; otherwise it is not a range we can honor.
      if (isIsoDate(from) && isIsoDate(to) && from <= to) {
        state.range = { preset: "custom", from, to };
      }
    } else {
      state.range = { preset, from: null, to: null };
    }
  }

  const rawGroupBy = sp.get("groupBy");
  if (rawGroupBy && (DIMENSIONS as readonly string[]).includes(rawGroupBy)) {
    state.groupBy = rawGroupBy as Dimension;
  }

  for (const dim of DIMENSIONS) {
    state.filters[dim] = parseMulti(sp.get(dim));
  }

  return state;
}

/**
 * Write a {@link FilterState} onto a copy of `base`, preserving every param `base` carries that this
 * module does not own (`tag`, `scope`, anything else). Defaults are omitted so the canonical view
 * has a clean URL and the round-trip holds at the defaults.
 */
export function filtersToSearchParams(
  state: FilterState,
  base?: URLSearchParams,
): URLSearchParams {
  // Start from base so tag/scope/etc. survive, then clear only the keys we own before rewriting them.
  const params = new URLSearchParams(base?.toString() ?? "");
  for (const key of MANAGED_KEYS) params.delete(key);

  if (state.range.preset !== DEFAULT_RANGE_PRESET) {
    params.set("range", state.range.preset);
    if (state.range.preset === "custom" && state.range.from && state.range.to) {
      params.set("from", state.range.from);
      params.set("to", state.range.to);
    }
  }

  if (state.groupBy !== DEFAULT_GROUP_BY) {
    params.set("groupBy", state.groupBy);
  }

  for (const dim of DIMENSIONS) {
    const values = state.filters[dim];
    if (values.length > 0) params.set(dim, values.join(","));
  }

  return params;
}

/** Convenience: the managed query string alone (no leading `?`), for building an API endpoint. */
export function filtersToQueryString(state: FilterState, base?: URLSearchParams): string {
  return filtersToSearchParams(state, base).toString();
}

// --- Small immutable state helpers, so the hook's setters stay one-liners ------------------------

export function withRangePreset(
  state: FilterState,
  preset: Exclude<TimeRangePreset, "custom">,
): FilterState {
  return { ...state, range: { preset, from: null, to: null } };
}

/** Set an explicit custom range. Rejects an invalid or reversed pair by returning `state` unchanged. */
export function withCustomRange(state: FilterState, from: string, to: string): FilterState {
  if (!isIsoDate(from) || !isIsoDate(to) || from > to) return state;
  return { ...state, range: { preset: "custom", from, to } };
}

export function withGroupBy(state: FilterState, groupBy: Dimension): FilterState {
  return { ...state, groupBy };
}

/** Toggle one value in a dimension's multi-select (add if absent, remove if present). */
export function toggleDimensionValue(
  state: FilterState,
  dim: Dimension,
  value: string,
): FilterState {
  const current = state.filters[dim];
  const next = current.includes(value)
    ? current.filter((v) => v !== value)
    : [...current, value];
  return { ...state, filters: { ...state.filters, [dim]: next } };
}

export function clearDimension(state: FilterState, dim: Dimension): FilterState {
  return { ...state, filters: { ...state.filters, [dim]: [] } };
}

export function clearAllFilters(state: FilterState): FilterState {
  return { ...state, filters: emptyFilters() };
}

/** How many days the range spans, inclusive. Presets are fixed; custom counts `from`..`to`. */
export function rangeDays(range: TimeRange): number {
  // DEFAULT_RANGE_PRESET is typed as the wide TimeRangePreset (it could in principle be "custom"),
  // so the 30-day fallback is spelled as the literal to keep it a valid PRESET_DAYS key.
  const fallbackDays = PRESET_DAYS["30d"];
  if (range.preset !== "custom") return PRESET_DAYS[range.preset];
  if (!range.from || !range.to) return fallbackDays;
  const from = Date.parse(`${range.from}T00:00:00Z`);
  const to = Date.parse(`${range.to}T00:00:00Z`);
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return fallbackDays;
  return Math.round((to - from) / 86_400_000) + 1;
}

/** True when no dimension filter is active, for an empty-state / "clear all" affordance. */
export function hasActiveFilters(state: FilterState): boolean {
  return DIMENSIONS.some((d) => state.filters[d].length > 0);
}
