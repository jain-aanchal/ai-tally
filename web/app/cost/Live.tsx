// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /cost (CTO-108, restyled onto the D1 design foundation in
// CTO-222/D2).
//
// WHAT THE DESIGN PASS KEEPS: every figure, column, banner and honest-blank the shipped page had.
// The three headline numbers (total, reconciled, estimated) move into SummaryTiles without changing
// what they read; the budget-vs-actual card, the burn-down forecast, the hidden-cost alerts and the
// by-feature table with its footer row are all still here, unchanged.
//
// WHAT IT ADDS:
//   - A FilterBar under the title (via PageHeader), URL-synced through useFilters.
//   - The static StackedBarChart is replaced by the interactive chart (tooltip, legend toggle,
//     click-to-drill). Click-to-drill adds a dimension filter through useFilters.
//
// FILTER-APPLICATION PATH (stated for the PR): the interactive chart honours the FULL filter set
// (window, group-by, dimension filters) through the /api/explore endpoint the foundation built for
// exactly this. At the DEFAULT slice (30 days, grouped by layer, no dimension filter) the chart is
// drawn from the existing /api/cost `series` payload, so the shipped per-layer numbers and the
// reconciled/estimated split are byte-for-byte what they were; any non-default slice fetches
// /api/explore and states honestly when the live source is unreachable rather than drawing a
// fabricated or zero-filled chart. The by-feature table honours the `feature` multi-select by hiding
// unselected rows (its footer re-totals over what is visible) — an honest hide, never a re-query
// against an endpoint that cannot take these filters. The three tiles stay the 30-day payload
// headline and are labelled as such.

"use client";

import { useEffect, useState } from "react";

import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { Card } from "@/components/Card";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { InteractiveStackedChart, type StackedChartDay } from "@/components/InteractiveStackedChart";
import { LiveIndicator } from "@/components/LiveIndicator";
import { PageHeader } from "@/components/PageHeader";
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
import type { ExploreSeries } from "@/lib/explore";
import { OTHER_GROUP } from "@/lib/explore";
import {
  DEFAULT_GROUP_BY,
  DEFAULT_RANGE_PRESET,
  DIMENSION_LABEL,
  hasActiveFilters,
  rangeDays,
} from "@/lib/filters";
import { formatUSD, type SpendByLayer } from "@/lib/types";
import { useFilters } from "@/lib/useFilters";
import { useLivePoll } from "@/lib/useLivePoll";

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

/** The live slice fetched from /api/explore for any non-default filter state. */
interface ExploreState {
  loading: boolean;
  /** "live" when a series came back, "unavailable" when ClickHouse could not be read, null when idle. */
  source: "live" | "unavailable" | null;
  series: ExploreSeries | null;
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
  // The time-range selector re-parameterises the headline tiles + By-feature table (CTO-226): the
  // managed query string (preserving any ?tag=/?scope=) rides on /api/cost, so 7d/30d/90d re-query
  // the window and useLivePoll re-fetches. The interactive chart is still served by /api/explore.
  const endpoint = queryString ? `/api/cost?${queryString}` : "/api/cost";
  const windowDays = rangeDays(filterState.range);
  const { data, updatedAt } = useLivePoll<CostPayload>(endpoint, initialData);
  const { series: costSeries, featureRows, alerts: hiddenCostAlerts } = data;

  const featureFilter = filterState.filters.feature;

  // The default slice is drawn from the shipped payload; anything else goes through /api/explore.
  const isDefaultSlice =
    filterState.range.preset === DEFAULT_RANGE_PRESET &&
    filterState.groupBy === DEFAULT_GROUP_BY &&
    !hasActiveFilters(filterState);

  const [explore, setExplore] = useState<ExploreState>({
    loading: false,
    source: null,
    series: null,
  });

  useEffect(() => {
    if (isDefaultSlice) {
      setExplore({ loading: false, source: null, series: null });
      return;
    }
    const ctrl = new AbortController();
    setExplore((e) => ({ ...e, loading: true }));
    fetch(`/api/explore?${queryString}`, { signal: ctrl.signal, cache: "no-store" })
      .then((r) => r.json())
      .then((j: { source: "live" | "unavailable"; series: ExploreSeries | null }) => {
        setExplore({ loading: false, source: j.source, series: j.series });
      })
      .catch((err: unknown) => {
        // An abort is expected on a fast filter change; keep the last state instead of flashing.
        if (err instanceof Error && err.name === "AbortError") return;
        // Honest-under-uncertainty: a failed fetch is "unavailable", never a zero-filled chart.
        setExplore({ loading: false, source: "unavailable", series: null });
      });
    return () => ctrl.abort();
  }, [isDefaultSlice, queryString]);

  const total = totalRange(costSeries);
  const reconciled = reconciledTotal(costSeries);
  const estimated = estimatedTotal(costSeries);
  const hasReconciledDate = costSeries.reconciledThrough > "1970-01-01";

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

  // Feature options for the FilterBar come from the payload the page already holds; layer options
  // are the fixed cost layers. The other dimensions (model/provider/account) are not known to
  // /api/cost, so the bar simply shows no control for them (never an empty menu), while group-by can
  // still key the interactive chart on them through /api/explore.
  const featureOptions: FilterOption[] = featureRows.map((r) => ({ value: r.feature }));
  const layerOptions: FilterOption[] = LAYERS.map((l) => ({ value: l, label: LAYER_LABEL[l] }));

  const visibleFeatureRows =
    featureFilter.length > 0
      ? featureRows.filter((r) => featureFilter.includes(r.feature))
      : featureRows;

  const chartTitle = isDefaultSlice
    ? "Cost by layer — last 30 days"
    : `Cost by ${DIMENSION_LABEL[filterState.groupBy].toLowerCase()} — ${sliceLabel(filterState.range.preset)}`;

  const body = (
    <div className="space-y-6">
      <TileGrid>
        <SummaryTile label="Total" micro={total} hint={`last ${windowDays} days`} />
        <SummaryTile
          label="Reconciled"
          micro={reconciled}
          hint={hasReconciledDate ? `through ${costSeries.reconciledThrough}` : "invoiced spend"}
        />
        <SummaryTile label="Estimated" micro={estimated} hint="not yet reconciled" />
      </TileGrid>

      <Card title={chartTitle}>
        <CostChart
          isDefaultSlice={isDefaultSlice}
          costSeries={costSeries}
          explore={explore}
          onDrillLayer={(g) => toggleFilter("layer", g)}
          onDrillGroup={(g) => {
            // The synthetic "other" fold is not a real dimension value, so it is not a filter.
            if (g === OTHER_GROUP) return;
            toggleFilter(filterState.groupBy, g);
          }}
        />
      </Card>

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

      <Card title="By feature">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Feature</th>
                {LAYERS.map((l) => (
                  <th key={l} className="py-1 text-right font-medium">
                    {LAYER_LABEL[l]}
                  </th>
                ))}
                <th className="py-1 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {visibleFeatureRows.map((r) => {
                const t = LAYERS.reduce((s, l) => s + r.byLayer[l], 0);
                return (
                  <tr key={r.feature} className="border-t border-edge">
                    <td className="py-2 font-medium">{r.feature}</td>
                    {LAYERS.map((l) => (
                      <td key={l} className="py-2 text-right tabular-nums">
                        {formatUSD(r.byLayer[l])}
                      </td>
                    ))}
                    <td className="py-2 text-right tabular-nums">{formatUSD(t)}</td>
                  </tr>
                );
              })}
              <FooterRow rows={visibleFeatureRows} />
            </tbody>
          </table>
        </div>
      </Card>
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
        toolbar={<FilterBar options={{ feature: featureOptions, layer: layerOptions }} />}
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
 * renders the explore series or states honestly that the live source is unavailable.
 */
function CostChart({
  isDefaultSlice,
  costSeries,
  explore,
  onDrillLayer,
  onDrillGroup,
}: {
  isDefaultSlice: boolean;
  costSeries: CostSeries;
  explore: ExploreState;
  onDrillLayer: (group: string) => void;
  onDrillGroup: (group: string) => void;
}) {
  if (isDefaultSlice) {
    const days: StackedChartDay[] = costSeries.days.map((d) => ({
      date: d.date,
      byGroup: { ...d.byLayer },
    }));
    return (
      <InteractiveStackedChart
        days={days}
        groups={LAYERS}
        color={(g) => LAYER_COLORS[g as Layer] ?? "#4c566a"}
        label={(g) => LAYER_LABEL[g as Layer] ?? g}
        onDrill={onDrillLayer}
        ariaLabel="stacked cost by layer over time"
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
  return (
    <InteractiveStackedChart
      days={days}
      groups={series.groups}
      onDrill={onDrillGroup}
      ariaLabel="stacked cost by selected dimension over time"
    />
  );
}

function FooterRow({ rows }: { rows: FeatureCostRow[] }) {
  const totals = LAYERS.reduce<SpendByLayer>(
    (acc, l) => {
      acc[l] = sumLayer(rows, l);
      return acc;
    },
    { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0 },
  );
  const grand = LAYERS.reduce((s, l) => s + totals[l], 0);
  return (
    <tr className="border-t border-edge bg-ink/40 font-medium">
      <td className="py-2">all features</td>
      {LAYERS.map((l) => (
        <td key={l} className="py-2 text-right tabular-nums">
          {formatUSD(totals[l])}
        </td>
      ))}
      <td className="py-2 text-right tabular-nums">{formatUSD(grand)}</td>
    </tr>
  );
}
