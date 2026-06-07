// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import { asOfLabel, boundaryFromMinutesAgo, deriveDataState, relativeAge } from "@/lib/dataState";
import { pctDelta, type Projection } from "@/lib/estimate";
import { formatUSD } from "@/lib/types";

export default async function EstimatePage() {
  const projection = await apiGet<Projection>("/api/estimate");
  const { workload, pr, current, proposed, blowUpRisk, drivers, sample } = projection;
  const costDelta = pctDelta(current.monthlyCostMicroUsd, proposed.monthlyCostMicroUsd);
  const p99Delta = pctDelta(current.p99CostMicroUsd, proposed.p99CostMicroUsd);
  const latDelta = pctDelta(current.meanLatencyMs, proposed.meanLatencyMs);
  const riskSeverity =
    blowUpRisk >= 0.3 ? "bad" : blowUpRisk >= 0.1 ? "warn" : "good";

  // This projection samples a reconciled historical window — surface that window's freshness so a
  // forecast off a stale baseline is never shown as fresh (CTO-80).
  const reconciledThrough = boundaryFromMinutesAgo(projection.reconcilerLastRunMinutesAgo);
  const noBaseline = current.monthlyCostMicroUsd === 0;
  const thinSample = sample.used > 0 && sample.pathologicalIncluded === 0;
  const state = deriveDataState({
    isEmpty: noBaseline,
    isPartial: thinSample,
    reconciledThrough,
  });
  const asOf = asOfLabel(reconciledThrough);

  const body = (
    <div className="space-y-6">
      {pr && (
        <div className="rounded-xl border border-edge bg-panel p-4 text-sm">
          <span className="text-muted">Estimating PR </span>
          <span className="font-mono text-accent">
            {pr.repo}#{pr.number}
          </span>
          <span className="text-muted"> — </span>
          <span>{pr.title}</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          label="Cost / month"
          current={formatUSD(current.monthlyCostMicroUsd)}
          proposed={formatUSD(proposed.monthlyCostMicroUsd)}
          delta={costDelta}
          betterWhenNegative
        />
        <Kpi
          label="p99 cost / run"
          current={formatUSD(current.p99CostMicroUsd)}
          proposed={formatUSD(proposed.p99CostMicroUsd)}
          delta={p99Delta}
          betterWhenNegative
          headline
        />
        <Kpi
          label="Blow-up risk"
          current="—"
          proposed={`${Math.round(blowUpRisk * 100)}%`}
          severity={riskSeverity}
          hint="P(p99 > 2× current)"
        />
        <Kpi
          label="Mean latency"
          current={`${current.meanLatencyMs} ms`}
          proposed={`${proposed.meanLatencyMs} ms`}
          delta={latDelta}
          betterWhenNegative
        />
      </div>

      <Card title="Driver breakdown">
        <ul className="space-y-2 text-sm">
          {drivers.map((d) => (
            <li key={d.reason} className="flex items-baseline justify-between gap-3">
              <span className="text-gray-300">{d.reason}</span>
              <span
                className={`tabular-nums font-medium ${d.delta > 0 ? "text-bad" : "text-good"}`}
              >
                {d.delta > 0 ? "+" : ""}
                {formatUSD(d.delta)}/mo
              </span>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Sample diagnostics">
        <dl className="grid grid-cols-1 gap-y-1.5 text-sm sm:grid-cols-2">
          <Diag k="samples used" v={`${sample.used} (${sample.tailWeighted} tail-weighted, ${sample.used - sample.tailWeighted} random)`} />
          <Diag k="pathological runs included" v={sample.pathologicalIncluded.toString()} good />
          <Diag k="confidence interval (on p99)" v={`±${Math.round(sample.ciHalfWidthPct * 100)}%`} />
          <Diag k="sampling strategy" v="tail-weighted (recommended)" good />
        </dl>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Estimate</h1>
          <p className="mt-1 text-sm text-muted">
            Workload: <span className="font-mono text-gray-300">{workload}</span>
          </p>
        </div>
        {state !== "empty" && asOf && (
          <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
        )}
      </div>

      {state === "partial" && <PartialDataBanner missing="tail-weighted sampling" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Estimate">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function Kpi({
  label,
  current,
  proposed,
  delta,
  betterWhenNegative,
  headline,
  severity,
  hint,
}: {
  label: string;
  current: string;
  proposed: string;
  delta?: number;
  betterWhenNegative?: boolean;
  headline?: boolean;
  severity?: "good" | "warn" | "bad";
  hint?: string;
}) {
  const ring =
    severity === "bad"
      ? "border-bad/40"
      : severity === "warn"
        ? "border-warn/40"
        : headline
          ? "border-accent/40"
          : "border-edge";
  const valueClass =
    severity === "bad" ? "text-bad" : severity === "warn" ? "text-warn" : "";
  return (
    <div className={`rounded-xl border ${ring} bg-panel p-5`}>
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-2 text-2xl font-semibold tabular-nums ${valueClass}`}>{proposed}</div>
      <div className="mt-1 text-xs text-muted">from {current}</div>
      {delta !== undefined && delta !== 0 && (
        <div
          className={`mt-2 text-sm tabular-nums ${
            (betterWhenNegative ? delta < 0 : delta > 0) ? "text-good" : "text-bad"
          }`}
        >
          {delta > 0 ? "+" : ""}
          {Math.round(delta * 100)}%
          {Math.abs(delta) >= 0.5 && (betterWhenNegative ? delta > 0 : delta < 0) ? " ⚠" : ""}
        </div>
      )}
      {hint && <div className="mt-2 text-xs text-muted">{hint}</div>}
    </div>
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
