// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /agents (CTO-108), rebuilt on the interactive design foundation
// (CTO-224, on the CTO-221 / CTO-220 primitives).
//
// The server component owns the first render: it fetches the payload, derives state, and renders
// the page. We then re-render the body block + StaleBadge using poll-updated data so users don't
// have to manually reload after each Aider response.
//
// CTO-224 additions, all design/interactivity only (no figure or honest-blank changed):
//   - PageHeader hosts the FilterBar (time range + group-by feature/model/provider + a feature
//     filter listing the agents), so a filtered view is shareable and the live chart honours it.
//   - SummaryTiles surface the money headlines the table used to bury (all-agent cost/day, the
//     priciest agent, the top outlier run, total flagged-outlier spend). Each is an aggregate of
//     figures already on the page, rendered through the honest Money primitive.
//   - ExploreChartCard adds the interactive cost-over-time-by-agent chart (tooltip, legend toggle,
//     click-to-drill) the page never had.
// The reconciled distribution table and pathological-runs list are preserved verbatim.

"use client";

import Link from "next/link";
import { Suspense } from "react";

import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { ExploreChartCard } from "@/components/ExploreChartCard";
import { FilterBar } from "@/components/FilterBar";
import { Histogram } from "@/components/Histogram";
import { LiveIndicator } from "@/components/LiveIndicator";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import { type AgentRun, type AgentSummary, p99Ratio } from "@/lib/agents";
import {
  asOfLabel,
  boundaryFromMinutesAgo,
  deriveDataState,
  relativeAge,
} from "@/lib/dataState";
import { formatUSD, type MicroUSD } from "@/lib/types";
import { useFilters } from "@/lib/useFilters";
import { useLivePoll } from "@/lib/useLivePoll";

export interface AgentsPayload {
  agents: AgentSummary[];
  runs: AgentRun[];
  // Real reconciler last-run in minutes (CTO-169), or null when the reconciler has never run /
  // the source is unavailable — rendered as `—` (no freshness badge) rather than a fake number.
  reconcilerLastRunMinutesAgo: number | null;
}

/** Max of a money field, or null when there is nothing to reduce (honest blank, never 0). */
function maxBy<T>(rows: T[], pick: (r: T) => MicroUSD): { value: MicroUSD; row: T } | null {
  if (rows.length === 0) return null;
  return rows.reduce<{ value: MicroUSD; row: T }>(
    (best, r) => {
      const v = pick(r);
      return v > best.value ? { value: v, row: r } : best;
    },
    { value: pick(rows[0]), row: rows[0] },
  );
}

export function AgentsLive({
  initialData,
}: {
  initialData: AgentsPayload;
}) {
  // The time-range selector re-parameterises the endpoint (CTO-226): the managed query string rides
  // on /api/agents (preserving any ?tag=/?run= deep link), so flipping 7d/30d/90d re-queries the
  // windowed cost/day average and useLivePoll re-fetches. See queryAgents for why cost/day is a
  // windowed daily average rather than a trailing-24h Node-clock sum.
  const { queryString } = useFilters();
  const endpoint = queryString ? `/api/agents?${queryString}` : "/api/agents";
  const { data, updatedAt } = useLivePoll<AgentsPayload>(endpoint, initialData);
  const { agents, runs, reconcilerLastRunMinutesAgo } = data;

  const reconciledThrough = boundaryFromMinutesAgo(reconcilerLastRunMinutesAgo);
  const noAgents = agents.length === 0 || agents.every((a) => a.costPerDayMicroUsd === 0);
  const someEmptyAgents =
    agents.some((a) => a.runsPerDay === 0) && agents.some((a) => a.runsPerDay > 0);
  const state = deriveDataState({
    isEmpty: noAgents,
    isPartial: someEmptyAgents,
    reconciledThrough,
  });
  const asOf = asOfLabel(reconciledThrough);

  // Money headlines: each is an aggregate of figures already rendered below, so no new number is
  // introduced. A null when there is nothing to reduce keeps the honest-blank posture over a fake 0.
  const totalCostPerDay = agents.reduce((s, a) => s + a.costPerDayMicroUsd, 0);
  const priciestAgent = maxBy(agents, (a) => a.costPerDayMicroUsd);
  const sortedRuns = runs.slice().sort((a, b) => b.totalCostMicroUsd - a.totalCostMicroUsd);
  const topOutlier = maxBy(runs, (r) => r.totalCostMicroUsd);
  const outlierTotal = runs.reduce((s, r) => s + r.totalCostMicroUsd, 0);

  // The FilterBar's feature filter lists the agents themselves (agents are features in the cost
  // model): filtering to one narrows the live chart to that agent's spend.
  const agentOptions = agents.map((a) => ({ value: a.name }));

  const body = (
    <div className="space-y-6">
      <TileGrid>
        <SummaryTile
          label="Cost/day · all agents"
          micro={agents.length > 0 ? totalCostPerDay : null}
          reason="no agent telemetry for this period"
          hint={`${agents.length} agent${agents.length === 1 ? "" : "s"}`}
        />
        <SummaryTile
          label="Priciest agent/day"
          micro={priciestAgent?.value ?? null}
          reason="no agent telemetry for this period"
          hint={priciestAgent?.row.name}
        />
        <SummaryTile
          label="Top outlier run"
          micro={topOutlier?.value ?? null}
          reason="no outlier runs captured"
          hint={topOutlier ? `${topOutlier.row.multipleOfMedian}× median` : undefined}
        />
        <SummaryTile
          label="Flagged outlier spend"
          micro={runs.length > 0 ? outlierTotal : null}
          reason="no outlier runs captured"
          hint={`${runs.length} run${runs.length === 1 ? "" : "s"}`}
        />
      </TileGrid>

      <ExploreChartCard
        title="Agent cost over time"
        groupByChoices={["feature", "model", "provider"]}
        defaultGroupBy="feature"
      />

      <Card title="Agent cost — distribution is the story">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">Agent</th>
              <th className="py-1 text-right font-medium">Runs/day</th>
              <th className="py-1 text-right font-medium">Cost/day</th>
              <th className="py-1 text-right font-medium">p50</th>
              <th className="py-1 text-right font-medium">p99</th>
              <th className="py-1 text-right font-medium">p99/p50</th>
              <th className="py-1 pl-4 text-left font-medium">Distribution</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => {
              const ratio = p99Ratio(a);
              const hot = ratio > 20;
              return (
                <tr key={a.name} className="border-t border-edge">
                  <td className="py-2 font-medium">{a.name}</td>
                  <td className="py-2 text-right tabular-nums">{a.runsPerDay.toLocaleString()}</td>
                  <td className="py-2 text-right tabular-nums">{formatUSD(a.costPerDayMicroUsd)}</td>
                  <td className="py-2 text-right tabular-nums">{formatUSD(a.p50MicroUsd)}</td>
                  <td className="py-2 text-right tabular-nums">{formatUSD(a.p99MicroUsd)}</td>
                  <td className="py-2 text-right tabular-nums">
                    <span className={hot ? "rounded bg-bad/20 px-1.5 py-0.5 text-bad" : ""}>
                      {ratio.toFixed(0)}×{hot ? " ⚠" : ""}
                    </span>
                  </td>
                  <td className="py-2 pl-4">
                    <Histogram buckets={a.distribution} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      <Card title="Pathological runs (top cost outliers)">
        <ul className="divide-y divide-edge text-sm">
          {sortedRuns.map((r) => (
            <li key={r.runId} className="flex items-center justify-between gap-3 py-2">
              <Link
                href={`/agents/runs/${r.runId}`}
                className="font-mono text-accent hover:underline"
              >
                {r.runId}
              </Link>
              <span className="flex items-center gap-3">
                <OutcomeBadge outcome={r.outcome} />
                <span className="tabular-nums">{formatUSD(r.totalCostMicroUsd)}</span>
                <span className="text-bad tabular-nums">{r.multipleOfMedian}× median</span>
              </span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Agents"
        subtitle="Per-agent cost, distribution, and the runs that blow the budget."
        actions={
          <>
            <LiveIndicator updatedAt={updatedAt} />
            {state !== "empty" && asOf && (
              <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
            )}
          </>
        }
        toolbar={
          <Suspense fallback={null}>
            <FilterBar
              groupByChoices={["feature", "model", "provider"]}
              defaultGroupBy="feature"
              options={{ feature: agentOptions }}
            />
          </Suspense>
        }
      />

      {state === "partial" && <PartialDataBanner missing="telemetry for some agents" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Agents">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function OutcomeBadge({ outcome }: { outcome: "success" | "failed" | "abandoned" }) {
  const cls =
    outcome === "success"
      ? "bg-good/20 text-good"
      : outcome === "failed"
        ? "bg-bad/20 text-bad"
        : "bg-edge text-muted";
  return <span className={`rounded px-1.5 py-0.5 text-xs ${cls}`}>{outcome}</span>;
}
