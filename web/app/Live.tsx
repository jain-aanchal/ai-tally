// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for the home dashboard (CTO-108, restyled onto the D1 design foundation
// in CTO-222/D2).
//
// The visual pass keeps every figure and every honest-blank rule the shipped page had: the four
// headline numbers (spend, estimated, reconciled, hidden cost) move into SummaryTiles WITHOUT
// changing what they read, the outlier / ROI / per-provider cards are untouched, and the data-state
// banners, LiveIndicator and StaleBadge all stay exactly where they were.
//
// FilterBar sits under the title via PageHeader. Home has no time-parameterised endpoint (its
// summary is the fixed 30-day roll-up), so the dimension multi-selects filter the rows the page
// already holds rather than re-querying: selecting a feature narrows the ROI snapshot, selecting a
// provider narrows the per-provider conversion table. That is an honest hide of rows, never a
// fabricated slice. Group-by is hidden here because Home has no grouped chart to re-key (see the
// interactive chart on /cost for the grouped case).

"use client";

import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { LiveIndicator } from "@/components/LiveIndicator";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import type { ProviderAttribution } from "@/lib/attribution";
import { LAYERS, type Layer } from "@/lib/cost";
import { allZero, asOfLabel, deriveDataState, relativeAge, zeroEnabledLayers } from "@/lib/dataState";
import type { CostOutlier, FeatureRoi, SpendSummary } from "@/lib/types";
import { formatUSD } from "@/lib/types";
import { useFilters } from "@/lib/useFilters";
import { useLivePoll } from "@/lib/useLivePoll";

export interface HomePayload {
  spend: SpendSummary;
  outliers: CostOutlier[];
  roi: FeatureRoi[];
  perProviderConversion: ProviderAttribution[];
}

export function HomeLive({
  endpoint,
  initialData,
  enabledLayers,
}: {
  endpoint: string;
  initialData: HomePayload;
  enabledLayers: readonly Layer[];
}) {
  const { data, updatedAt } = useLivePoll<HomePayload>(endpoint, initialData);
  const { spend: s, outliers, roi, perProviderConversion } = data;

  // Same URL-synced filter state the FilterBar writes. Home reads it to narrow the tables it holds.
  const { state: filterState } = useFilters();
  const featureFilter = filterState.filters.feature;
  const providerFilter = filterState.filters.provider;

  const featureOptions: FilterOption[] = roi.map((r) => ({ value: r.feature }));
  const providerOptions: FilterOption[] = perProviderConversion.map((p) => ({ value: p.provider }));

  const visibleRoi =
    featureFilter.length > 0 ? roi.filter((r) => featureFilter.includes(r.feature)) : roi;
  const visibleProviders =
    providerFilter.length > 0
      ? perProviderConversion.filter((p) => providerFilter.includes(p.provider))
      : perProviderConversion;

  // Hidden cost is every non-LLM layer: the "all-in" story the tile makes explicit. The percentage
  // and its zero-when-empty behaviour are carried over verbatim (CTO-222 keeps the meaning as-is).
  const hidden =
    s.byLayer.vector + s.byLayer.tools + s.byLayer.compute + s.byLayer.embeddings + s.byLayer.egress;
  const hiddenPct = s.totalMicroUsd === 0 ? 0 : Math.round((hidden / s.totalMicroUsd) * 100);

  const layers: Record<string, number> = { ...s.byLayer };
  const layerTotals = LAYERS.reduce<Record<Layer, number>>(
    (acc, l) => {
      acc[l] = s.byLayer[l];
      return acc;
    },
    { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0 },
  );
  const trippedLayers = zeroEnabledLayers(layerTotals, enabledLayers);
  const state = deriveDataState({
    isEmpty: s.totalMicroUsd === 0 && allZero(layers),
    isPartial: trippedLayers.length > 0,
    reconciledThrough: s.reconciledThrough,
  });
  const asOf = asOfLabel(s.reconciledThrough);
  const hasReconciledDate = s.reconciledThrough > "1970-01-01";

  const tiles = (
    <TileGrid>
      <SummaryTile label="Spend" micro={s.totalMicroUsd} hint="last 30 days" higherIsBetter={false} />
      <SummaryTile label="Estimated" micro={s.estimatedMicroUsd} hint="not yet reconciled" />
      <SummaryTile
        label="Reconciled"
        micro={s.reconciledMicroUsd}
        hint={hasReconciledDate ? `through ${s.reconciledThrough}` : "invoiced spend"}
      />
      <SummaryTile
        label="Hidden cost"
        micro={hidden}
        hint={`${hiddenPct}% of spend · vector + tools + compute`}
      />
    </TileGrid>
  );

  const grid = (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card title="Top cost outliers (30d)">
        <ul className="space-y-2 text-sm">
          {outliers.map((o) => (
            <li key={o.runId} className="flex items-center justify-between gap-3">
              <span className="truncate font-mono text-gray-300">{o.runId}</span>
              <span className="shrink-0">
                <span className="font-medium">{formatUSD(o.costMicroUsd)}</span>{" "}
                <span className="text-bad">{o.multipleOfMedian}× median</span>
              </span>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="ROI snapshot">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">Feature</th>
              <th className="py-1 text-right font-medium">Cost/user</th>
              <th className="py-1 text-right font-medium">Value/user</th>
              <th className="py-1 text-right font-medium">Payback</th>
            </tr>
          </thead>
          <tbody>
            {visibleRoi.map((r) => (
              <tr key={r.feature} className="border-t border-edge">
                <td className="py-1.5">{r.feature}</td>
                <td className="py-1.5 text-right">{formatUSD(r.costPerUserMicroUsd)}</td>
                <td className="py-1.5 text-right">
                  {r.valuePerUserMicroUsd === null ? (
                    <span className="text-muted">—</span>
                  ) : (
                    formatUSD(r.valuePerUserMicroUsd)
                  )}
                </td>
                <td className="py-1.5 text-right">
                  {r.paybackDays === null ? (
                    <span className="text-muted">unattributed</span>
                  ) : (
                    `${r.paybackDays}d`
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Per-provider · conversion">
        {perProviderConversion.length === 0 ? (
          <p className="text-sm text-muted">
            No sessions yet — drive traffic to populate (link out from{" "}
            <a className="text-good underline" href="/attribution">
              Attribution
            </a>
            ).
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Provider</th>
                <th className="py-1 text-right font-medium">Sessions</th>
                <th className="py-1 text-right font-medium">Conversions</th>
                <th className="py-1 text-right font-medium">Rate</th>
                <th className="py-1 text-right font-medium">$/conversion</th>
              </tr>
            </thead>
            <tbody>
              {visibleProviders.map((p) => (
                <tr key={p.provider} className="border-t border-edge">
                  <td className="py-1.5 font-mono">{p.provider}</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {p.sessions.toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {p.conversions.toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {(p.conversionRate * 100).toFixed(1)}%
                  </td>
                  <td className="py-1.5 text-right font-semibold tabular-nums">
                    {p.costPerConversionMicroUsd === null
                      ? "—"
                      : formatUSD(p.costPerConversionMicroUsd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );

  const body = (
    <div className="space-y-6">
      {tiles}
      {grid}
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Home"
        subtitle="What your AI costs, and whether it pays for itself"
        actions={
          <>
            <LiveIndicator updatedAt={updatedAt} />
            {state !== "empty" && asOf && (
              <StaleBadge
                asOf={asOf}
                age={relativeAge(s.reconciledThrough)}
                stale={state === "stale"}
              />
            )}
          </>
        }
        toolbar={
          <FilterBar
            options={{ feature: featureOptions, provider: providerOptions }}
            hideGroupBy
          />
        }
      />

      {state === "partial" && <PartialDataBanner trippedLayers={trippedLayers} />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Home">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}
