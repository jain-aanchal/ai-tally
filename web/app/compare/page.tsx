// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
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

  const body = (
    <div className="space-y-6">
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
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Compare</h1>
          <p className="mt-1 text-sm text-muted">
            Workload: <span className="font-mono text-gray-300">{workload}</span>
          </p>
        </div>
        {state !== "empty" && asOf && (
          <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
        )}
      </div>

      {state === "partial" && <PartialDataBanner missing="the replay sampler" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Compare">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function Row({
  label,
  m,
  current,
  highlight,
}: {
  label: string;
  m: { monthlyCostMicroUsd: MicroUSD; qualityScore: number; latencyP95Ms: number; errorRate: number };
  current?: { monthlyCostMicroUsd: MicroUSD; qualityScore: number; latencyP95Ms: number; errorRate: number };
  highlight?: boolean;
}) {
  return (
    <tr className={`border-t border-edge ${highlight ? "font-medium" : ""}`}>
      <td className="py-2">{label}</td>
      <td className="py-2 text-right tabular-nums">
        {formatUSD(m.monthlyCostMicroUsd)}
        {current && <Delta v={deltaPct(current.monthlyCostMicroUsd, m.monthlyCostMicroUsd)} betterWhenNegative />}
      </td>
      <td className="py-2 text-right tabular-nums">
        {(m.qualityScore * 100).toFixed(1)}%
        {current && <DeltaPp v={(m.qualityScore - current.qualityScore) * 100} betterWhenPositive />}
      </td>
      <td className="py-2 text-right tabular-nums">
        {m.latencyP95Ms} ms
        {current && <Delta v={deltaPct(current.latencyP95Ms, m.latencyP95Ms)} betterWhenNegative />}
      </td>
      <td className="py-2 text-right tabular-nums">
        {(m.errorRate * 100).toFixed(2)}%
        {current && <DeltaPp v={(m.errorRate - current.errorRate) * 100} betterWhenPositive={false} />}
      </td>
    </tr>
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
