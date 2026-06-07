// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { Histogram } from "@/components/Histogram";
import { type AgentRun, type AgentSummary, p99Ratio } from "@/lib/agents";
import { apiGet } from "@/lib/api";
import { asOfLabel, boundaryFromMinutesAgo, deriveDataState, relativeAge } from "@/lib/dataState";
import { formatUSD } from "@/lib/types";

interface AgentsPayload {
  agents: AgentSummary[];
  runs: AgentRun[];
  reconcilerLastRunMinutesAgo: number;
}

export default async function AgentsPage() {
  const { agents, runs, reconcilerLastRunMinutesAgo } =
    await apiGet<AgentsPayload>("/api/agents");

  // Agents presents reconciled per-agent cost, so it carries a freshness boundary like Cost/Features.
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

  const body = (
    <div className="space-y-6">
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
          {runs
            .slice()
            .sort((a, b) => b.totalCostMicroUsd - a.totalCostMicroUsd)
            .map((r) => (
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
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Agents</h1>
        {state !== "empty" && asOf && (
          <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
        )}
      </div>

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
