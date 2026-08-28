// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /cost (CTO-108, restyled onto the D1 design foundation in
// CTO-222/D2, turned into the unified Cost explorer in CTO-239/CTO-240 M1).
//
// WHAT M1 CHANGES (CTO-240): the tiles, the chart, and the breakdown table now all respond to the
// FULL filter set (window + group-by + dimension filters + a client-side search), not just the
// window. The gap it closes: the FilterBar writes `?feature=` but the old tiles came off /api/cost,
// which only read the window and the legacy `?tag=`, so a Feature filter narrowed the table while
// the headline number sat unchanged. The tiles now read the filter-aware slice totals from
// /api/explore, so they move with the whole filter set.
//
// DATA SOURCES (three, each honest on its own):
//   - /api/cost (polled): the hidden-cost alerts, the per-layer default chart series, and the
//     enabled-layers / partial-data state. Its per-layer series still draws the DEFAULT chart
//     byte-for-byte, and its mock fallback keeps the offline / synthetic-preview view working.
//   - /api/explore (fetched on every filter change): the grouped time series, the per-group
//     breakdown rows, and the slice tile totals. No mock fallback by design: an arbitrary live slice
//     that cannot be read is stated, never zero-filled.
//   - /api/cost/budget (fetched once per render): the budget-vs-actual and burn-down cards, kept
//     exactly as they were (they change on a human/daily cadence, not a 5-second one).
//
// KEPT UNCHANGED: the budget-vs-actual card, the burn-down forecast, the hidden-cost alerts, and the
// `?tag=`/`?scope=` carry-through.

"use client";

import { useEffect, useMemo, useState } from "react";

import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { Card } from "@/components/Card";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { InteractiveStackedChart, type StackedChartDay } from "@/components/InteractiveStackedChart";
import { LiveIndicator } from "@/components/LiveIndicator";
import { Blank, Money, Pct } from "@/components/HonestValue";
import { PageHeader } from "@/components/PageHeader";
import { Sparkline } from "@/components/Sparkline";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import {
  LAYER_COLORS,
  LAYER_LABEL,
  LAYERS,
  type CostSeries,
  estimatedTotal,
  type FeatureCostRow,
  type HiddenCostAlert,
  type Layer,
  reconciledTotal,
  totalRange,
} from "@/lib/cost";
import { asOfLabel, deriveDataState, relativeAge, zeroEnabledLayers } from "@/lib/dataState";
import type {
  CostSliceTotals,
  ExploreBreakdownPrior,
  ExploreBreakdownRow,
  ExploreDayPoint,
  ExploreSeries,
} from "@/lib/explore";
import { costTrend, OTHER_GROUP } from "@/lib/explore";
import {
  DEFAULT_GROUP_BY,
  DEFAULT_RANGE_PRESET,
  DIMENSION_LABEL,
  type Dimension,
  hasActiveFilters,
  rangeDays,
} from "@/lib/filters";
import { formatUSD } from "@/lib/types";
import { useFilters } from "@/lib/useFilters";
import { useLivePoll } from "@/lib/useLivePoll";

import { AgentDetail } from "./AgentDetail";
import { FeatureDetail } from "./FeatureDetail";
import { BudgetVsActualCard } from "./BudgetVsActualCard";
import { BurndownCard, type CostBudgetPayload } from "./BurndownCard";

export interface CostPayload {
  series: CostSeries;
  featureRows: FeatureCostRow[];
  alerts: HiddenCostAlert[];
}

function sumLayer(rows: FeatureCostRow[], layer: Layer) {
  return rows.reduce((s, r) => s + r.byLayer[layer], 0);
}

/** The live slice fetched from /api/explore for the active filter state. */
interface ExploreState {
  loading: boolean;
  /** "live" when a series came back, "unavailable" when ClickHouse could not be read, null when idle. */
  source: "live" | "unavailable" | null;
  series: ExploreSeries | null;
  /** Filter-aware headline totals for the tiles. null when unreachable (rendered as honest blank). */
  totals: CostSliceTotals | null;
  /** Prior-window per-group totals for the breakdown trend column (CTO-244). null blanks the trend
   *  cells (prior read failed or slice idle) while the rest of the table still renders. */
  breakdownPrior: ExploreBreakdownPrior | null;
}

/**
 * Display label for a group value under the active group-by. Layers get their human label (so a
 * group-by=layer view reads "LLM" / "Tool calls" like the shipped layer view); every other dimension
 * carries its raw value (a model id, a provider, a feature tag), and the synthetic "other" fold keeps
 * its own namespaced label.
 */
function groupLabel(groupBy: Dimension, value: string): string {
  if (value === OTHER_GROUP) return value;
  if (groupBy === "layer") return LAYER_LABEL[value as Layer] ?? value;
  return value;
}

export function CostLive({
  initialData,
  enabledLayers,
  budget,
  tag = null,
}: {
  initialData: CostPayload;
  enabledLayers: readonly Layer[];
  // Not part of the polled payload (CTO-209/210): a budget changes about monthly and the settled
  // window advances once a day, so re-reading both every 5 seconds would be pure waste. See the
  // comment at the top of app/api/cost/budget/route.ts.
  budget: CostBudgetPayload;
  /** The active ?tag= breakdown filter, carried so a scope change does not silently drop it. */
  tag?: string | null;
}) {
  const { state: filterState, toggleFilter, queryString } = useFilters();
  // The time-range selector re-parameterises the /api/cost payload (CTO-226): the managed query
  // string (preserving any ?tag=/?scope=) rides on /api/cost, so 7d/30d/90d re-query the window and
  // useLivePoll re-fetches. The tiles and breakdown come from /api/explore (CTO-240).
  const endpoint = queryString ? `/api/cost?${queryString}` : "/api/cost";
  const windowDays = rangeDays(filterState.range);
  const { data, updatedAt } = useLivePoll<CostPayload>(endpoint, initialData);
  const { series: costSeries, featureRows, alerts: hiddenCostAlerts } = data;

  // The default slice keeps the shipped per-layer chart + tile numbers where /api/explore is idle or
  // unreachable; every slice still fetches /api/explore for the filter-aware totals and breakdown.
  const isDefaultSlice =
    filterState.range.preset === DEFAULT_RANGE_PRESET &&
    filterState.groupBy === DEFAULT_GROUP_BY &&
    !hasActiveFilters(filterState);

  // Case-insensitive substring search over the breakdown group values. Client-side only (a transient
  // view tweak, not a shareable filter): it narrows the breakdown rows AND the chart bands.
  const [search, setSearch] = useState("");

  const [explore, setExplore] = useState<ExploreState>({
    loading: false,
    source: null,
    series: null,
    totals: null,
    breakdownPrior: null,
  });

  useEffect(() => {
    // Every filter change re-fetches the filter-aware slice (CTO-240): the tiles must move with the
    // whole filter set, not just the window, so this runs even on the default slice.
    const ctrl = new AbortController();
    setExplore((e) => ({ ...e, loading: true }));
    fetch(`/api/explore?${queryString}`, { signal: ctrl.signal, cache: "no-store" })
      .then((r) => r.json())
      .then(
        (j: {
          source: "live" | "unavailable";
          series: ExploreSeries | null;
          totals: CostSliceTotals | null;
          breakdownPrior: ExploreBreakdownPrior | null;
        }) => {
          setExplore({
            loading: false,
            source: j.source,
            series: j.series,
            totals: j.totals ?? null,
            breakdownPrior: j.breakdownPrior ?? null,
          });
        },
      )
      .catch((err: unknown) => {
        // An abort is expected on a fast filter change; keep the last state instead of flashing.
        if (err instanceof Error && err.name === "AbortError") return;
        // Honest-under-uncertainty: a failed fetch is "unavailable", never a zero-filled slice.
        setExplore({
          loading: false,
          source: "unavailable",
          series: null,
          totals: null,
          breakdownPrior: null,
        });
      });
    return () => ctrl.abort();
  }, [queryString]);

  // Shipped /api/cost figures, kept as the DEFAULT-slice fallback for the tiles so the offline / mock
  // / synthetic-preview view still renders today's numbers when /api/explore is idle or unreachable.
  const total = totalRange(costSeries);
  const reconciled = reconciledTotal(costSeries);
  const estimated = estimatedTotal(costSeries);

  const layerTotals = LAYERS.reduce<Record<Layer, number>>(
    (acc, l) => {
      acc[l] = sumLayer(featureRows, l);
      return acc;
    },
    { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0 },
  );
  const trippedLayers = zeroEnabledLayers(layerTotals, enabledLayers);
  const state = deriveDataState({
    isEmpty: total === 0,
    isPartial: trippedLayers.length > 0,
    reconciledThrough: costSeries.reconciledThrough,
  });
  const asOf = asOfLabel(costSeries.reconciledThrough);

  // Tile values: the filter-aware slice totals when /api/explore answered, else the shipped
  // /api/cost figures on the default slice (so the byte-for-byte default and the offline view hold),
  // else an honest blank (null) on a non-default slice we could not read — never a zero.
  const slice = explore.totals;
  const tileTotal = slice ? slice.totalMicroUsd : isDefaultSlice ? total : null;
  const tileReconciled = slice ? slice.reconciledMicroUsd : isDefaultSlice ? reconciled : null;
  const tileEstimated = slice ? slice.estimatedMicroUsd : isDefaultSlice ? estimated : null;
  const tileReconciledThrough = slice ? slice.reconciledThrough : costSeries.reconciledThrough;
  const hasReconciledDate = tileReconciledThrough > "1970-01-01";
  const sliceUnavailable = explore.source === "unavailable" && !isDefaultSlice;
  const tileReason = sliceUnavailable
    ? "this slice is served live and the telemetry source could not be reached"
    : "no cost data for this slice";

  // Feature options for the FilterBar come from the payload the page already holds; layer options
  // are the fixed cost layers. The other dimensions (model/provider/account) are not enumerated by
  // /api/cost, so the bar shows no filter control for them (never an empty menu), while group-by can
  // still key the chart, breakdown and tiles on them through /api/explore.
  const featureOptions: FilterOption[] = featureRows.map((r) => ({ value: r.feature }));
  const layerOptions: FilterOption[] = LAYERS.map((l) => ({ value: l, label: LAYER_LABEL[l] }));

  // The breakdown group-by: whatever /api/explore grouped by, or the URL's group-by while it loads.
  const breakdownGroupBy: Dimension = explore.series?.groupBy ?? filterState.groupBy;

  // Agent filter options (CTO-241): /api/cost doesn't enumerate agents, but the explore breakdown
  // does once grouped by agent, so the FilterBar offers an Agent dropdown listing the agents in the
  // slice. The synthetic "other" fold is not a real agent, so it is never a filterable option.
  const agentOptions: FilterOption[] =
    breakdownGroupBy === "agent" && explore.series
      ? explore.series.breakdown
          .filter((r) => r.group !== OTHER_GROUP)
          .map((r) => ({ value: r.group }))
      : [];

  // The single-agent detail is shown only when the explorer groups by agent AND is narrowed to
  // exactly one agent (CTO-241): that is the slice for which one agent's run distribution and
  // pathological runs are meaningful. It reuses the /agents data + components (see AgentDetail).
  const singleAgent =
    filterState.groupBy === "agent" && filterState.filters.agent.length === 1
      ? filterState.filters.agent[0]
      : null;

  // The single-feature detail is shown only when the explorer groups by feature AND is narrowed to
  // exactly one feature (CTO-242, M3): that is the slice for which one feature's unit economics,
  // value-event config and attribution diagnostics are meaningful. It reuses the /features data +
  // components (see FeatureDetail) so nothing the retired Features tab showed for a feature is lost.
  const singleFeature =
    filterState.groupBy === "feature" && filterState.filters.feature.length === 1
      ? filterState.filters.feature[0]
      : null;

  // Breakdown rows: from /api/explore when present; else, on the default slice, a per-layer fallback
  // off the /api/cost layer totals so the offline / synthetic-preview view keeps a table.
  const breakdownRows: ExploreBreakdownRow[] = useMemo(() => {
    if (explore.series) return explore.series.breakdown;
    if (isDefaultSlice) {
      return LAYERS.map((l) => ({ group: l, totalMicroUsd: layerTotals[l], spanCount: 0 }));
    }
    return [];
    // layerTotals is recomputed each render from featureRows; depend on its values, not identity.
  }, [explore.series, isDefaultSlice, layerTotals]);

  // The search predicate matches the raw group value OR its display label, so "tool" finds the
  // `tools` layer / "Tool calls", and it narrows the breakdown rows and the chart bands identically.
  const q = search.trim().toLowerCase();
  const matchesSearch = useMemo(
    () => (value: string) =>
      q === "" ||
      value.toLowerCase().includes(q) ||
      groupLabel(breakdownGroupBy, value).toLowerCase().includes(q),
    [q, breakdownGroupBy],
  );

  const chartTitle = isDefaultSlice
    ? "Cost by layer — last 30 days"
    : `Cost by ${DIMENSION_LABEL[filterState.groupBy].toLowerCase()} — ${sliceLabel(filterState.range.preset)}`;

  const body = (
    <div className="space-y-6">
      <TileGrid>
        <SummaryTile
          label="Total"
          micro={tileTotal}
          reason={tileReason}
          hint={`last ${windowDays} days`}
        />
        <SummaryTile
          label="Reconciled"
          micro={tileReconciled}
          reason={tileReason}
          hint={hasReconciledDate ? `through ${tileReconciledThrough}` : "invoiced spend"}
        />
        <SummaryTile
          label="Estimated"
          micro={tileEstimated}
          reason={tileReason}
          hint="not yet reconciled"
        />
      </TileGrid>

      <Card title={chartTitle}>
        <CostChart
          isDefaultSlice={isDefaultSlice}
          costSeries={costSeries}
          explore={explore}
          matchesSearch={matchesSearch}
          onDrillLayer={(g) => toggleFilter("layer", g)}
          onDrillGroup={(g) => {
            // The synthetic "other" fold is not a real dimension value, so it is not a filter.
            if (g === OTHER_GROUP) return;
            toggleFilter(filterState.groupBy, g);
          }}
        />
      </Card>

      {/* The values behind the chart, directly under it: one row per group with its cost and share,
          plus the search box that narrows both the rows and the chart bands above. Sits here rather
          than at the foot of the page so a reader can read the number for any bar without scrolling
          past the budget and forecast cards. */}
      <BreakdownTable
        groupBy={breakdownGroupBy}
        rows={breakdownRows}
        prior={explore.breakdownPrior}
        days={explore.series?.days ?? null}
        search={search}
        onSearch={setSearch}
        matchesSearch={matchesSearch}
        unavailable={sliceUnavailable}
      />

      {/* Directly under the 30-day headline on purpose (CTO-209): this card's figure is smaller
          than that total, and the two only reconcile once you have read the coverage line saying
          which days it counted. Putting them far apart is how the page starts looking broken. */}
      <BudgetVsActualCard payload={budget} />

      {/* Immediately after the measured card (CTO-210). The order is the argument: what happened,
          then where it lands. Putting the projection first would let a reader take a forecast as a
          fact, and the two cards share a settled window that only makes sense read in that order. */}
      <BurndownCard
        payload={budget.forecast}
        scoped={budget.scoped}
        scopeHref={(key) =>
          `/cost?${new URLSearchParams(tag ? { tag, scope: key } : { scope: key }).toString()}`
        }
      />

      {hiddenCostAlerts.map((a) => (
        <div
          key={a.message}
          className={`rounded-xl border p-4 text-sm ${
            a.severity === "warn"
              ? "border-warn/40 bg-warn/10 text-warn"
              : "border-edge bg-panel text-muted"
          }`}
        >
          <span className="font-medium">Hidden cost: </span>
          {a.message}
        </div>
      ))}

      {/* The retired /agents view, preserved for one agent (CTO-241): when grouping by agent and
          narrowed to a single agent, its run distribution + pathological runs render here. */}
      {singleAgent && <AgentDetail agent={singleAgent} queryString={queryString} />}

      {/* The retired /features view, preserved for one feature (CTO-242): when grouping by feature
          and narrowed to a single feature, its unit economics, value-event config and attribution
          diagnostics render here. */}
      {singleFeature && <FeatureDetail feature={singleFeature} />}
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cost"
        subtitle="Where the spend goes, by layer and by feature"
        actions={
          <>
            <LiveIndicator updatedAt={updatedAt} />
            {state !== "empty" && asOf && (
              <StaleBadge
                asOf={asOf}
                age={relativeAge(costSeries.reconciledThrough)}
                stale={state === "stale"}
              />
            )}
          </>
        }
        toolbar={
          <FilterBar
            options={{ feature: featureOptions, layer: layerOptions, agent: agentOptions }}
          />
        }
      />

      {state === "partial" && <PartialDataBanner trippedLayers={trippedLayers} />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Cost">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

/** A short human label for the active window preset, used in the chart card title. */
function sliceLabel(preset: string): string {
  switch (preset) {
    case "7d":
      return "last 7 days";
    case "90d":
      return "last 90 days";
    case "custom":
      return "custom range";
    default:
      return "last 30 days";
  }
}

/**
 * The interactive chart. Two data sources, one component: the default slice maps the shipped
 * CostSeries onto the generic chart keyed by layer (colours and labels unchanged), any other slice
 * renders the explore series or states honestly that the live source is unavailable. Either way the
 * search box narrows the rendered bands to the matching groups.
 */
function CostChart({
  isDefaultSlice,
  costSeries,
  explore,
  matchesSearch,
  onDrillLayer,
  onDrillGroup,
}: {
  isDefaultSlice: boolean;
  costSeries: CostSeries;
  explore: ExploreState;
  matchesSearch: (value: string) => boolean;
  onDrillLayer: (group: string) => void;
  onDrillGroup: (group: string) => void;
}) {
  if (isDefaultSlice) {
    const days: StackedChartDay[] = costSeries.days.map((d) => ({
      date: d.date,
      byGroup: { ...d.byLayer },
    }));
    const groups = LAYERS.filter((l) => matchesSearch(l));
    return (
      <InteractiveStackedChart
        days={days}
        groups={groups}
        color={(g) => LAYER_COLORS[g as Layer] ?? "#4c566a"}
        label={(g) => LAYER_LABEL[g as Layer] ?? g}
        onDrill={onDrillLayer}
        ariaLabel="stacked cost by layer over time"
        emptyLabel="no layers match the search"
      />
    );
  }

  if (explore.loading && explore.series === null) {
    return <p className="py-8 text-center text-sm text-muted">Loading this slice…</p>;
  }

  if (explore.source === "unavailable" || explore.series === null) {
    // Honest-under-uncertainty: a live slice we cannot read is stated, never zero-filled.
    return (
      <p className="py-8 text-center text-sm text-muted">
        This slice is served live and the telemetry source could not be reached, so no chart is
        drawn. The default 30-day view above works without it.
      </p>
    );
  }

  const series = explore.series;
  const days: StackedChartDay[] = series.days.map((d) => ({ date: d.date, byGroup: d.byGroup }));
  const groups = series.groups.filter((g) => matchesSearch(g));
  return (
    <InteractiveStackedChart
      days={days}
      groups={groups}
      onDrill={onDrillGroup}
      ariaLabel="stacked cost by selected dimension over time"
      emptyLabel="no groups match the search"
    />
  );
}

/**
 * The general "By {dimension}" breakdown table (CTO-240, extended in CTO-244): one row per group
 * value with its cost, its share of the shown total, its trend vs the prior equal-length window, and
 * a daily-cost sparkline. Cost and % of total are sortable (they share a denominator, so they order
 * identically, but each header is its own affordance). A footer re-totals exactly the rows on screen.
 * A client-side search box narrows the rows (and, via the shared predicate, the chart bands above).
 * When there is nothing to show the table states why (loading, unavailable, or no match).
 *
 * Every added column is honest under uncertainty (CTO-244): share blanks when the shown total is
 * zero; trend renders a "new" marker for a group with no prior spend and blanks when the prior read
 * was unavailable or the row is the aggregated "other" tail, never a fabricated percent or a
 * divide-by-zero; the sparkline is omitted for a group with fewer than two days of spend rather than
 * drawing a degenerate line.
 */
function BreakdownTable({
  groupBy,
  rows,
  prior,
  days,
  search,
  onSearch,
  matchesSearch,
  unavailable,
}: {
  groupBy: Dimension;
  rows: ExploreBreakdownRow[];
  /** Prior-window per-group totals keyed by raw group value; null blanks every trend cell. */
  prior: ExploreBreakdownPrior | null;
  /** The chart's own day×group series, reused for the per-row sparkline; null omits sparklines. */
  days: readonly ExploreDayPoint[] | null;
  search: string;
  onSearch: (value: string) => void;
  matchesSearch: (value: string) => boolean;
  unavailable: boolean;
}) {
  // Default sort is cost desc (the big spenders first, like the shipped by-feature table); either
  // sortable header toggles direction. % of total shares cost's denominator so it orders the same
  // way, but it is its own column so a reader can sort from the share header directly. Search is
  // applied before the sort so the footer totals what is shown.
  const [sortKey, setSortKey] = useState<"cost" | "share">("cost");
  const [dir, setDir] = useState<"desc" | "asc">("desc");
  const visible = rows
    .filter((r) => matchesSearch(r.group))
    .sort((a, b) =>
      dir === "desc" ? b.totalMicroUsd - a.totalMicroUsd : a.totalMicroUsd - b.totalMicroUsd,
    );
  // The share denominator is the footer total (the rows on screen, CTO-244), so shares always sum to
  // 100% of what the footer shows; guarded so a zero total renders an honest blank, not a divide.
  const footerTotal = visible.reduce((s, r) => s + r.totalMicroUsd, 0);
  const dimLabel = DIMENSION_LABEL[groupBy].toLowerCase();

  const toggleSort = (key: "cost" | "share") => {
    if (key === sortKey) {
      setDir((d) => (d === "desc" ? "asc" : "desc"));
    } else {
      setSortKey(key);
      setDir("desc");
    }
  };
  const sortArrow = (key: "cost" | "share") =>
    key === sortKey ? (dir === "desc" ? "▼" : "▲") : "";

  return (
    <Card title={`By ${dimLabel}`}>
      <div className="mb-3 flex items-center gap-2">
        <input
          type="search"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={`Search ${dimLabel}…`}
          aria-label={`Search ${dimLabel}`}
          className="w-56 rounded-md border border-edge bg-ink px-2 py-1 text-sm text-fg focus:border-accent focus:outline-none"
        />
        {search && (
          <button
            type="button"
            onClick={() => onSearch("")}
            className="rounded-md px-2 py-1 text-xs text-muted hover:text-fg"
          >
            Clear
          </button>
        )}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[36rem] text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">{DIMENSION_LABEL[groupBy]}</th>
              <th className="py-1 text-right font-medium">
                <button
                  type="button"
                  onClick={() => toggleSort("cost")}
                  className="inline-flex items-center gap-1 uppercase hover:text-fg"
                  aria-label={`Sort by cost ${sortKey === "cost" && dir === "desc" ? "ascending" : "descending"}`}
                >
                  Cost
                  <span aria-hidden>{sortArrow("cost")}</span>
                </button>
              </th>
              <th className="py-1 text-right font-medium">
                <button
                  type="button"
                  onClick={() => toggleSort("share")}
                  className="inline-flex items-center gap-1 uppercase hover:text-fg"
                  aria-label={`Sort by share of total ${sortKey === "share" && dir === "desc" ? "ascending" : "descending"}`}
                >
                  % of total
                  <span aria-hidden>{sortArrow("share")}</span>
                </button>
              </th>
              <th className="py-1 text-right font-medium">Trend</th>
              <th className="py-1 text-right font-medium">30d</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr className="border-t border-edge">
                <td colSpan={5} className="py-6 text-center text-muted">
                  {unavailable
                    ? "This slice is served live and the telemetry source could not be reached."
                    : search
                      ? `No ${dimLabel} matches “${search}”.`
                      : `No ${dimLabel} spend in this slice.`}
                </td>
              </tr>
            ) : (
              <>
                {visible.map((r) => (
                  <tr key={r.group} className="border-t border-edge">
                    <td className="py-2 font-medium">{groupLabel(groupBy, r.group)}</td>
                    <td className="py-2 text-right tabular-nums">{formatUSD(r.totalMicroUsd)}</td>
                    <td className="py-2 text-right tabular-nums">
                      {footerTotal > 0 ? (
                        <Pct value={r.totalMicroUsd / footerTotal} />
                      ) : (
                        <Blank reason="no spend in this slice, so there is no total to take a share of" />
                      )}
                    </td>
                    <td className="py-2 text-right">
                      <TrendCell group={r.group} current={r.totalMicroUsd} prior={prior} />
                    </td>
                    <td className="py-2">
                      <div className="flex justify-end">
                        <RowSparkline group={r.group} days={days} />
                      </div>
                    </td>
                  </tr>
                ))}
                <tr className="border-t border-edge bg-ink/40 font-medium">
                  <td className="py-2">
                    {search ? `all shown ${dimLabel}` : `all ${dimLabel}`}
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    <Money micro={footerTotal} />
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {footerTotal > 0 ? (
                      <Pct value={1} />
                    ) : (
                      <Blank reason="no spend in this slice" />
                    )}
                  </td>
                  <td className="py-2" />
                  <td className="py-2" />
                </tr>
              </>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/**
 * The trend cell for one breakdown row (CTO-244): this window's cost for the group vs the prior
 * equal-length window, as a ▲/▼ + percent. Cost trend colouring is inverted from a value metric: up
 * is bad, down is good, flat is muted. Honest under uncertainty on three branches: the aggregated
 * "other" tail has no single prior group to compare, a null prior map means the prior read was
 * unavailable, and a group with no prior spend is genuinely "new" rather than an up-infinity percent.
 */
function TrendCell({
  group,
  current,
  prior,
}: {
  group: string;
  current: number;
  prior: ExploreBreakdownPrior | null;
}) {
  if (group === OTHER_GROUP) {
    return <Blank reason="aggregated tail, no single prior group to compare" />;
  }
  if (prior === null) {
    return <Blank reason="prior window unavailable, no comparison" />;
  }
  const trend = costTrend(current, prior[group]);
  if (trend.kind === "new") {
    // A real marker, not a fabricated percent: this group had no spend in the prior window.
    return (
      <span
        title="new this period, no prior window to compare"
        className="cursor-help rounded-full bg-accent/10 px-1.5 py-0.5 text-[11px] font-medium uppercase text-accent"
      >
        new
      </span>
    );
  }
  const rising = trend.fraction > 0;
  const flat = trend.fraction === 0;
  // Cost: up is bad, down is good, flat is muted.
  const tone = flat ? "text-muted" : rising ? "text-bad" : "text-good";
  const arrow = flat ? "→" : rising ? "▲" : "▼";
  return (
    <span className={`inline-flex items-center justify-end gap-1 tabular-nums ${tone}`}>
      <span aria-hidden>{arrow}</span>
      <Pct value={Math.abs(trend.fraction)} />
    </span>
  );
}

/**
 * A per-row daily-cost sparkline (CTO-244) off the chart's own day×group series, so it adds no query.
 * A group with fewer than two days of spend has no meaningful line, so it renders a flat dash rather
 * than a degenerate or broken SVG.
 */
function RowSparkline({
  group,
  days,
}: {
  group: string;
  days: readonly ExploreDayPoint[] | null;
}) {
  if (!days) return <span className="text-muted">{"—"}</span>;
  const values = days.map((d) => d.byGroup[group] ?? 0);
  const daysWithSpend = values.filter((v) => v > 0).length;
  if (daysWithSpend < 2) return <span className="text-muted">{"—"}</span>;
  return <Sparkline values={values} width={80} height={20} ariaLabel={`daily cost for ${group}`} />;
}
