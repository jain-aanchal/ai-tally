// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for the home dashboard (CTO-108).

"use client";

import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { LiveIndicator } from "@/components/LiveIndicator";
import { LAYERS, type Layer } from "@/lib/cost";
import { allZero, asOfLabel, deriveDataState, relativeAge, zeroEnabledLayers } from "@/lib/dataState";
import type { CostOutlier, FeatureRoi, SpendSummary } from "@/lib/types";
import { formatUSD } from "@/lib/types";
import { useLivePoll } from "@/lib/useLivePoll";

export interface HomePayload {
  spend: SpendSummary;
  outliers: CostOutlier[];
  roi: FeatureRoi[];
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
  const { spend: s, outliers, roi } = data;

  const hidden = s.byLayer.vector + s.byLayer.tools + s.byLayer.compute + s.byLayer.embeddings + s.byLayer.egress;
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

  const grid = (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card title="Spend — last 30 days">
        <div className="text-3xl font-semibold">{formatUSD(s.totalMicroUsd)}</div>
        <div className="mt-2 text-sm text-muted">
          estimated {formatUSD(s.estimatedMicroUsd)} · reconciled {formatUSD(s.reconciledMicroUsd)}
          <span className="ml-1 text-xs">(through {s.reconciledThrough})</span>
        </div>
        <div className="mt-3 text-sm text-warn">
          Hidden cost: {formatUSD(hidden)} ({hiddenPct}%) — vector + tools + compute
        </div>
      </Card>

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
            {roi.map((r) => (
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

    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Home</h1>
        <div className="flex items-center gap-2">
          <LiveIndicator updatedAt={updatedAt} />
          {state !== "empty" && asOf && (
            <StaleBadge asOf={asOf} age={relativeAge(s.reconciledThrough)} stale={state === "stale"} />
          )}
        </div>
      </div>

      {state === "partial" && <PartialDataBanner trippedLayers={trippedLayers} />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Home">{grid}</SyntheticPreviewBanner>
      ) : (
        grid
      )}
    </div>
  );
}

