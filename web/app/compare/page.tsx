// SPDX-License-Identifier: Apache-2.0
// Cross-provider Compare, rebuilt on the interactive design foundation (CTO-224, on CTO-221 /
// CTO-220). Design + interactivity only: the candidates table (with its CTO-114/115/123 honest
// blank cells), the recommendation card and the replay diagnostics are preserved verbatim. Added:
// the FilterBar (time range + group-by model/provider + model/provider filters), the money
// headlines as SummaryTiles, and the interactive cost-over-time-by-model chart.
import { Suspense } from "react";

import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { ExploreChartCard } from "@/components/ExploreChartCard";
import { FilterBar } from "@/components/FilterBar";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import { apiGet } from "@/lib/api";
import { type Comparison, deltaPct } from "@/lib/compare";
import { asOfLabel, boundaryFromMinutesAgo, deriveDataState, relativeAge } from "@/lib/dataState";
import { formatUSD, type MicroUSD } from "@/lib/types";

export default async function ComparePage({
  searchParams,
}: {
  searchParams?: Promise<{ tag?: string }>;
}) {
  // Parse ?tag= for URL stability across the CTO-104 deep-link set. The /api/compare data is
  // mock-only today, so the filter is captured but doesn't yet narrow the comparison — CTO-105
  // will wire it through to a tag-scoped replay.
  await searchParams;
  const comparison = await apiGet<Comparison>("/api/compare");
  const { workload, current, candidates, recommendation, diagnostics } = comparison;

  // This projection is built off reconciled baseline traffic — surface that baseline's freshness so
  // a comparison off a stale window is never shown as fresh (CTO-80).
  const reconciledThrough = boundaryFromMinutesAgo(diagnostics.reconcilerLastRunMinutesAgo);
  const noBaseline = current.monthlyCostMicroUsd === 0 || candidates.length === 0;
  const noReplay = diagnostics.samplesReplayed === 0 && diagnostics.samplesAvailable > 0;
  const state = deriveDataState({
    isEmpty: noBaseline,
    isPartial: noReplay,
    reconciledThrough,
  });
  const asOf = asOfLabel(reconciledThrough);

  // Money headlines from the payload. Cheapest candidate is a genuine min over real figures; null
  // when there are no candidates (honest blank rather than a fabricated 0).
  const cheapest =
    candidates.length > 0
      ? candidates.reduce((best, c) =>
          c.monthlyCostMicroUsd < best.monthlyCostMicroUsd ? c : best,
        )
      : null;
  const savingsPct = Math.round(recommendation.projectedSavingsPct * 100);

  // FilterBar options: the models in play and their distinct providers, so the live chart can be
  // narrowed to a model or a provider.
  const modelOptions = [current, ...candidates].map((m) => ({ value: m.model }));
  const providerOptions = Array.from(new Set([current, ...candidates].map((m) => m.provider))).map(
    (p) => ({ value: p }),
  );

  const body = (
    <div className="space-y-6">
      <TileGrid>
        <SummaryTile
          label="Current cost/mo"
          micro={current.monthlyCostMicroUsd}
          hint={current.model}
        />
        <SummaryTile
          label="Cheapest candidate/mo"
          micro={cheapest?.monthlyCostMicroUsd ?? null}
          reason="no candidate cleared replay yet"
          hint={cheapest?.model}
        />
        <SummaryTile
          label="Projected savings/mo"
          micro={recommendation.projectedSavingsMicroUsd}
          higherIsBetter
          hint={`${savingsPct}% reduction`}
        />
        <SummaryTile
          label="Replay cost"
          micro={diagnostics.replayCostMicroUsd}
          hint={`${diagnostics.samplesReplayed.toLocaleString()} traces replayed`}
        />
      </TileGrid>

      <ExploreChartCard
        title="Model cost over time"
        groupByChoices={["model", "provider"]}
        defaultGroupBy="model"
      />

      <Card title="Candidates vs. current">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Model</th>
                <th className="py-1 text-right font-medium">Cost/mo</th>
                <th className="py-1 text-right font-medium">Quality</th>
                <th className="py-1 text-right font-medium">Latency p95</th>
                <th className="py-1 text-right font-medium">Error rate</th>
              </tr>
            </thead>
            <tbody>
              <Row label={`current · ${current.model}`} m={current} highlight />
              {candidates.map((c) => (
                <Row key={c.model} label={c.model} m={c} current={current} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={`Recommendation — ${recommendation.verdict}`}>
        <p className="text-sm text-gray-200">{recommendation.summary}</p>
        <div className="mt-3 flex items-baseline gap-2 text-sm">
          <span className="text-good text-lg font-semibold">
            saves {formatUSD(recommendation.projectedSavingsMicroUsd)}/mo
          </span>
          <span className="text-muted">
            ({Math.round(recommendation.projectedSavingsPct * 100)}% reduction)
          </span>
        </div>
        <button
          type="button"
          className="mt-3 rounded-md border border-edge bg-ink px-3 py-1.5 text-sm text-gray-200 hover:bg-edge"
        >
          Export routing rule
        </button>
      </Card>

      <Card title="Replay diagnostics">
        <dl className="grid grid-cols-1 gap-y-1.5 text-sm sm:grid-cols-2">
          <Diag k="samples replayed" v={`${diagnostics.samplesReplayed.toLocaleString()} of ${diagnostics.samplesAvailable.toLocaleString()} prod traces`} />
          <Diag k="excluded (rate limits)" v={diagnostics.excludedRateLimited.toLocaleString()} />
          <Diag k="replay cost" v={formatUSD(diagnostics.replayCostMicroUsd)} />
          <Diag k="context fidelity" v={diagnostics.contextFidelity} good />
        </dl>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Compare"
        subtitle={
          <>
            Workload: <span className="font-mono text-gray-300">{workload}</span>
          </>
        }
        actions={
          state !== "empty" && asOf ? (
            <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
          ) : undefined
        }
        toolbar={
          <Suspense fallback={null}>
            <FilterBar
              groupByChoices={["model", "provider"]}
              defaultGroupBy="model"
              options={{ model: modelOptions, provider: providerOptions }}
            />
          </Suspense>
        }
      />

      {state === "partial" && <PartialDataBanner missing="the replay sampler" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Compare">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

type RowMetric = {
  monthlyCostMicroUsd: MicroUSD;
  qualityScore: number | null; // CTO-114
  qualityCi?: { lo: number; hi: number }; // CTO-114
  latencyP95Ms: number | null; // CTO-115
  errorRate: number | null; // CTO-115
};

function Row({
  label,
  m,
  current,
  highlight,
}: {
  label: string;
  m: RowMetric;
  current?: RowMetric;
  highlight?: boolean;
}) {
  // CTO-115: latency/error on the live `current` row are null when fewer than 50 spans landed
  // in the 7-day window. Render "—" rather than fabricating a placeholder, and skip deltas
  // against a null baseline.
  const lowSampleTitle = "needs ≥50 spans in 7d";
  return (
    <tr className={`border-t border-edge ${highlight ? "font-medium" : ""}`}>
      <td className="py-2">{label}</td>
      <td className="py-2 text-right tabular-nums">
        {formatUSD(m.monthlyCostMicroUsd)}
        {current && <Delta v={deltaPct(current.monthlyCostMicroUsd, m.monthlyCostMicroUsd)} betterWhenNegative />}
      </td>
      <td className="py-2 text-right tabular-nums">
        <QualityCell m={m} current={current} />
      </td>
      <td className="py-2 text-right tabular-nums">
        {m.latencyP95Ms === null ? (
          <span className="text-muted" title={lowSampleTitle}>
            —
          </span>
        ) : (
          <>{m.latencyP95Ms} ms</>
        )}
        {current && current.latencyP95Ms !== null && m.latencyP95Ms !== null && (
          <Delta v={deltaPct(current.latencyP95Ms, m.latencyP95Ms)} betterWhenNegative />
        )}
      </td>
      <td className="py-2 text-right tabular-nums">
        {m.errorRate === null ? (
          <span className="text-muted" title={lowSampleTitle}>
            —
          </span>
        ) : (
          <>{(m.errorRate * 100).toFixed(2)}%</>
        )}
        {current && current.errorRate !== null && m.errorRate !== null && (
          <DeltaPp v={(m.errorRate - current.errorRate) * 100} betterWhenPositive={false} />
        )}
      </td>
    </tr>
  );
}

// CTO-114: quality cell renders the real pairwise-LLM-judge win-rate when present, with the
// Wilson 95% CI underneath in muted text (matches Attribution's confidence display). When
// `qualityScore` is null — n < 10 judged samples, or no eval pass has run — show "—" with a
// hover hint. Deliberately no fallback to mock; the ticket is explicit.
function QualityCell({ m, current }: { m: RowMetric; current?: RowMetric }) {
  if (m.qualityScore === null) {
    return (
      <span className="text-muted" title="needs ≥10 judged samples — run eval pass">
        —
      </span>
    );
  }
  return (
    <div className="flex flex-col items-end leading-tight">
      <span>
        {(m.qualityScore * 100).toFixed(1)}%
        {current && current.qualityScore !== null && (
          <DeltaPp v={(m.qualityScore - current.qualityScore) * 100} betterWhenPositive />
        )}
      </span>
      {m.qualityCi && (
        <span className="text-xs text-muted">
          [{Math.round(m.qualityCi.lo * 100)}–{Math.round(m.qualityCi.hi * 100)}%]
        </span>
      )}
    </div>
  );
}

function Delta({ v, betterWhenNegative }: { v: number; betterWhenNegative: boolean }) {
  if (v === 0) return null;
  const good = betterWhenNegative ? v < 0 : v > 0;
  const sign = v > 0 ? "+" : "";
  return (
    <span className={`ml-1 text-xs ${good ? "text-good" : "text-bad"}`}>
      {sign}
      {Math.round(v * 100)}%
    </span>
  );
}

function DeltaPp({ v, betterWhenPositive }: { v: number; betterWhenPositive: boolean }) {
  if (Math.abs(v) < 0.05) return null;
  const good = betterWhenPositive ? v > 0 : v < 0;
  const sign = v > 0 ? "+" : "";
  return (
    <span className={`ml-1 text-xs ${good ? "text-good" : "text-bad"}`}>
      {sign}
      {v.toFixed(1)}pp
    </span>
  );
}

function Diag({ k, v, good }: { k: string; v: string; good?: boolean }) {
  return (
    <>
      <dt className="text-muted">{k}</dt>
      <dd className={good ? "text-good" : ""}>{v}</dd>
    </>
  );
}
