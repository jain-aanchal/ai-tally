// SPDX-License-Identifier: Apache-2.0
// The single source of truth pages read for the active filter slice (CTO-221, D1).
//
// State lives in the URL query string, not in React state: `useSearchParams` is the read side and a
// shallow `router.replace` is the write side, so a filtered view is shareable, the browser back
// button walks the filter history, and two components on the same page (a chart and a toolbar) stay
// in lockstep because they both read the URL rather than a local copy that could drift.
//
// `replace` (not `push`) with `scroll: false`: flipping a filter should not stack a new history
// entry per keystroke nor jump the viewport. Every write goes through `commit`, which rebuilds the
// query from the CURRENT params (so a concurrent `tag`/`scope` is preserved, see filters.ts) and
// hands the setters as one-liners over the pure state helpers.

"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import {
  type Dimension,
  type FilterState,
  type TimeRangePreset,
  clearAllFilters,
  clearDimension,
  filtersToSearchParams,
  parseFilters,
  toggleDimensionValue,
  withCustomRange,
  withGroupBy,
  withRangePreset,
} from "./filters";

export interface UseFiltersResult {
  /** The parsed filter state, reconstructed from the URL on every navigation. */
  state: FilterState;
  setRangePreset: (preset: Exclude<TimeRangePreset, "custom">) => void;
  setCustomRange: (from: string, to: string) => void;
  setGroupBy: (dim: Dimension) => void;
  toggleFilter: (dim: Dimension, value: string) => void;
  clearFilter: (dim: Dimension) => void;
  clearAll: () => void;
  /** The managed query string for the current state (no leading `?`), for building an endpoint. */
  queryString: string;
}

export function useFilters(): UseFiltersResult {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  // A fresh URLSearchParams each render: ReadonlyURLSearchParams is not directly mutable and we must
  // not retain a reference that a later `delete` could mutate under the parser.
  const currentParams = useMemo(
    () => new URLSearchParams(searchParams?.toString() ?? ""),
    [searchParams],
  );

  const state = useMemo(() => parseFilters(currentParams), [currentParams]);
  const queryString = useMemo(
    () => filtersToSearchParams(state, currentParams).toString(),
    [state, currentParams],
  );

  const commit = useCallback(
    (next: FilterState) => {
      const qs = filtersToSearchParams(next, currentParams).toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname, currentParams],
  );

  const setRangePreset = useCallback(
    (preset: Exclude<TimeRangePreset, "custom">) => commit(withRangePreset(state, preset)),
    [commit, state],
  );
  const setCustomRange = useCallback(
    (from: string, to: string) => commit(withCustomRange(state, from, to)),
    [commit, state],
  );
  const setGroupBy = useCallback((dim: Dimension) => commit(withGroupBy(state, dim)), [commit, state]);
  const toggleFilter = useCallback(
    (dim: Dimension, value: string) => commit(toggleDimensionValue(state, dim, value)),
    [commit, state],
  );
  const clearFilter = useCallback(
    (dim: Dimension) => commit(clearDimension(state, dim)),
    [commit, state],
  );
  const clearAll = useCallback(() => commit(clearAllFilters(state)), [commit, state]);

  return {
    state,
    setRangePreset,
    setCustomRange,
    setGroupBy,
    toggleFilter,
    clearFilter,
    clearAll,
    queryString,
  };
}
