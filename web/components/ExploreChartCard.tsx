// SPDX-License-Identifier: Apache-2.0
// The filter-aware interactive cost chart the analysis pages mount (CTO-224, building on the CTO-221
// / CTO-220 design foundation). One client component, reused by /agents, /compare, /features and
// /unit-economics, so every page gets the same interactive surface (tooltip, legend toggle,
// click-to-drill) wired to the SAME URL filters the FilterBar writes.
//
// WHY it reads /api/explore and not each page's own endpoint: the reconciled per-page endpoints
// (/api/agents, /api/compare, ...) return a fixed reconciled slice and do not take the filter query.
// The explore endpoint is the foundation's filter-aware surface: it parses the exact range / group-by
// / dimension-filter params the FilterBar owns, so mounting it here is what makes an active filter
// actually change what the page shows. The page's reconciled tables below stay their own source of
// truth; this card is the live, sliceable view over the top.
//
// Honest-under-uncertainty: /api/explore returns `source: "unavailable"` with a null series when
// ClickHouse is unreachable (there is deliberately no mock fallback for arbitrary live slices). We
// render an explained blank in that case, never a fabricated or zero-filled chart.

"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  InteractiveStackedChart,
  type StackedChartDay,
} from "@/components/InteractiveStackedChart";
import {
  DIMENSION_LABEL,
  type Dimension,
  filtersToQueryString,
  withGroupBy,
} from "@/lib/filters";
import { formatUSD, type MicroUSD } from "@/lib/types";
import { useFilters } from "@/lib/useFilters";

interface ExploreBreakdownRow {
  group: string;
  totalMicroUsd: MicroUSD;
  spanCount: number;
}

interface ExploreSeries {
  groupBy: Dimension;
  windowStart: string;
  windowEnd: string;
  windowDays: number;
  groups: string[];
  days: StackedChartDay[];
  breakdown: ExploreBreakdownRow[];
  totalMicroUsd: MicroUSD;
  truncatedGroups: number;
}

interface ExploreResponse {
  source: "live" | "unavailable";
  groupBy: Dimension;
  series: ExploreSeries | null;
}

export function ExploreChartCard({
  title,
  groupByChoices,
  defaultGroupBy,
}: {
  title: string;
  /** The group-by dimensions this page offers, mirroring the page's FilterBar. */
  groupByChoices?: readonly Dimension[];
  /** The group-by to use when the URL carries none this page offers (mirrors the FilterBar). */
  defaultGroupBy?: Dimension;
}) {
  const { state, toggleFilter } = useFilters();
  const searchParams = useSearchParams();
  const [resp, setResp] = useState<ExploreResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  // Resolve the group-by exactly the way the FilterBar does, so the very first fetch already groups
  // by the page's dimension instead of the global `layer` default the FilterBar is still committing.
  // The chart and the toolbar therefore never disagree, not even for the first paint.
  const effectiveGroupBy =
    defaultGroupBy && groupByChoices && !groupByChoices.includes(state.groupBy)
      ? defaultGroupBy
      : state.groupBy;
  const queryString = useMemo(() => {
    const base = new URLSearchParams(searchParams?.toString() ?? "");
    const effectiveState =
      effectiveGroupBy === state.groupBy ? state : withGroupBy(state, effectiveGroupBy);
    return filtersToQueryString(effectiveState, base);
  }, [state, effectiveGroupBy, searchParams]);

  // Refetch whenever the managed query string changes (a range flip, a group-by change, a dimension
  // filter toggle) so the chart always reflects the URL. AbortController cancels an in-flight request
  // when the filters change again mid-flight, so a slow response can't clobber a newer one.
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setLoading(true);
    setFailed(false);
    fetch(`/api/explore?${queryString}`, { signal: ctrl.signal, cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`explore ${r.status}`);
        return r.json() as Promise<ExploreResponse>;
      })
      .then((j) => {
        if (cancelled) return;
        setResp(j);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return;
        if (cancelled) return;
        setFailed(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [queryString]);

  const series = resp?.series ?? null;
  const unavailable = failed || resp?.source === "unavailable" || series === null;

  return (
    <section className="rounded-xl border border-edge bg-panel p-5">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted">{title}</h2>
        {series && (
          <span className="text-xs text-muted">
            by {DIMENSION_LABEL[series.groupBy].toLowerCase()} ·{" "}
            <span className="tabular-nums text-gray-200">{formatUSD(series.totalMicroUsd)}</span> ·{" "}
            <span className="tabular-nums">
              {series.windowStart} → {series.windowEnd}
            </span>
          </span>
        )}
      </div>

      {loading && !series ? (
        <p className="py-10 text-center text-sm text-muted">Loading breakdown…</p>
      ) : unavailable ? (
        // Honest blank: no live source for this slice, with the reason on hover. Never a fake chart.
        <p
          className="cursor-help py-10 text-center text-sm text-muted underline decoration-dotted decoration-muted/60 underline-offset-4"
          title="ClickHouse is unreachable, so this live slice has no data. It is left blank rather than fabricated."
        >
          Live breakdown unavailable for this slice
        </p>
      ) : (
        <>
          <InteractiveStackedChart
            days={series.days}
            groups={series.groups}
            ariaLabel={`cost over time by ${DIMENSION_LABEL[series.groupBy].toLowerCase()}`}
            // Click a bar segment or legend swatch to filter on the grouped dimension. exploreParams
            // excludes a dimension's own filter while it is the group-by (see explore.ts), so the
            // drill lands as a URL filter chip; switching Group by then reveals that slice.
            onDrill={(group) => toggleFilter(effectiveGroupBy, group)}
          />
          {series.truncatedGroups > 0 && (
            <p className="mt-2 text-xs text-muted">
              {series.truncatedGroups} smaller{" "}
              {DIMENSION_LABEL[series.groupBy].toLowerCase()}
              {series.truncatedGroups === 1 ? "" : "s"} folded into “· other”.
            </p>
          )}
        </>
      )}
    </section>
  );
}
