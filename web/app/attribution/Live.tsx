// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /attribution (CTO-108).

"use client";

import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { LiveIndicator } from "@/components/LiveIndicator";
import type { AttributionReport, ProviderAttribution } from "@/lib/attribution";
import { formatUSD } from "@/lib/types";
import { useLivePoll } from "@/lib/useLivePoll";

export function AttributionLive({
  endpoint,
  initialData,
  outcome,
  tag,
  provider,
}: {
  endpoint: string;
  initialData: AttributionReport;
  outcome: string;
  tag: string | null;
  provider: string | null;
}) {
  const { data: report, updatedAt } = useLivePoll<AttributionReport>(endpoint, initialData);

  const body = (
    <div className="space-y-6">
      <Card title="Headline">
        <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <Headline k="sessions" v={report.totals.sessions.toLocaleString()} />
          <Headline
            k={`${outcome} events`}
            v={report.totals.conversions.toLocaleString()}
          />
          <Headline k="LLM cost" v={formatUSD(report.totals.costMicroUsd)} />
          <Headline
            k={`$ / ${outcome}`}
            v={
              report.totals.costPerConversionMicroUsd === null
                ? "—"
                : formatUSD(report.totals.costPerConversionMicroUsd)
            }
            highlight
          />
        </dl>
      </Card>

      <Card title={`Per-provider · ${outcome}`}>
        {report.perProvider.length === 0 ? (
          <p className="text-sm text-muted">
            No sessions match these filters yet. Run{" "}
            <code className="rounded bg-ink px-1 py-0.5 text-xs">
              make chatbot-demo
            </code>{" "}
            from <code className="text-xs">infra/</code> to drive synthetic traffic.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr>
                  <th className="py-1 text-left font-medium">Provider</th>
                  <th className="py-1 text-right font-medium">Sessions</th>
                  <th className="py-1 text-right font-medium">{outcome}s</th>
                  <th className="py-1 text-right font-medium">Rate (95% CI)</th>
                  <th className="py-1 text-right font-medium">LLM cost</th>
                  <th className="py-1 text-right font-medium">$/{outcome}</th>
                  <th className="py-1 text-right font-medium">Value/user</th>
                  <th className="py-1 text-right font-medium">Margin/user</th>
                </tr>
              </thead>
              <tbody>
                {report.perProvider.map((p) => (
                  <ProviderRow key={p.provider} p={p} outcome={outcome} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3 text-xs text-muted">
          Intervals are Wilson 95% on the conversion rate — small samples produce
          wide bands, by design. Two providers &ldquo;tie&rdquo; when their bands overlap.
        </p>
      </Card>

      <Card title="Filters">
        <dl className="grid grid-cols-2 gap-y-1 text-sm sm:grid-cols-3">
          <Filter k="tag" v={tag ?? "(all)"} />
          <Filter k="provider" v={provider ?? "(all)"} />
          <Filter k="outcome" v={outcome} />
        </dl>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Attribution</h1>
          <p className="mt-1 text-sm text-muted">
            $/{outcome} per provider, joined from LLM spans and CDP events on{" "}
            <span className="font-mono">UserIdHash</span>.
          </p>
        </div>
        <LiveIndicator updatedAt={updatedAt} />
      </div>
      {report.isMock ? (
        <SyntheticPreviewBanner workflow="Attribution">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function Headline({
  k,
  v,
  highlight,
}: {
  k: string;
  v: string;
  highlight?: boolean;
}) {
  return (
    <div>
      <dt className="text-xs uppercase text-muted">{k}</dt>
      <dd
        className={`mt-0.5 tabular-nums ${highlight ? "text-lg font-semibold text-good" : "text-base"}`}
      >
        {v}
      </dd>
    </div>
  );
}

function ProviderRow({
  p,
  outcome,
}: {
  p: ProviderAttribution;
  outcome: string;
}) {
  return (
    <tr className="border-t border-edge">
      <td className="py-2 font-mono">{p.provider}</td>
      <td className="py-2 text-right tabular-nums">{p.sessions.toLocaleString()}</td>
      <td className="py-2 text-right tabular-nums">{p.conversions.toLocaleString()}</td>
      <td className="py-2 text-right tabular-nums">
        {(p.conversionRate * 100).toFixed(1)}%{" "}
        <span className="text-xs text-muted">
          [{(p.conversionRateLo * 100).toFixed(1)}–
          {(p.conversionRateHi * 100).toFixed(1)}%]
        </span>
      </td>
      <td className="py-2 text-right tabular-nums">{formatUSD(p.costMicroUsd)}</td>
      <td className="py-2 text-right font-semibold tabular-nums">
        {p.costPerConversionMicroUsd === null
          ? "—"
          : formatUSD(p.costPerConversionMicroUsd)}
        <span className="sr-only"> per {outcome}</span>
      </td>
      <td className="py-2 text-right tabular-nums">
        {p.valuePerUserMicroUsd === null ? "—" : formatUSD(p.valuePerUserMicroUsd)}
      </td>
      <td className="py-2 text-right tabular-nums">
        {p.marginPerUserMicroUsd === null ? (
          "—"
        ) : (
          <>
            <div
              className={
                p.marginPerUserMicroUsd >= 0
                  ? "font-semibold text-good"
                  : "font-semibold text-warn"
              }
            >
              {formatUSD(p.marginPerUserMicroUsd)}
            </div>
            {p.marginPct !== null && (
              <div className="text-xs text-muted">
                {(p.marginPct * 100).toFixed(1)}%
              </div>
            )}
          </>
        )}
      </td>
    </tr>
  );
}

function Filter({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="text-muted">{k}</dt>
      <dd className="font-mono">{v}</dd>
    </>
  );
}
