// SPDX-License-Identifier: Apache-2.0
// The single-agent detail block inside the unified Cost explorer (CTO-241, M2 of CTO-239).
//
// When /cost groups by `agent` AND is filtered to exactly one agent, this renders that agent's run
// distribution below the breakdown table, preserving everything the retired /agents view showed for
// one agent: the log-scale distribution histogram, p50 / p99 / p99÷p50 ratio, the pathological-runs
// list linking to the run drill-down, and the reconciler-freshness badge.
//
// It reuses the SAME source and shapes as /agents rather than reimplementing: a tenant-scoped fetch
// of /api/agents?agent=<ServiceName> (queryAgents, filtered to that agent by ServiceName), the
// AgentSummary / AgentRun types and p99Ratio from lib/agents, the Histogram component, and the
// dataState / StaleBadge freshness primitives. Honest-under-uncertainty throughout: a source we
// cannot reach and an agent with no runs each state why, never a fabricated or zero figure.

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Card } from "@/components/Card";
import { StaleBadge } from "@/components/DataStateBanner";
import { Histogram } from "@/components/Histogram";
import { type AgentRun, type AgentSummary, p99Ratio } from "@/lib/agents";
import {
  asOfLabel,
  boundaryFromMinutesAgo,
  deriveDataState,
  relativeAge,
} from "@/lib/dataState";
import { formatUSD } from "@/lib/types";

interface AgentsDetailPayload {
  agents: AgentSummary[];
  runs: AgentRun[];
  reconcilerLastRunMinutesAgo: number | null;
}

type FetchStatus = "loading" | "ready" | "unavailable";

/**
 * @param agent   the selected agent's ServiceName (filters.agent[0]).
 * @param queryString the managed filter query string, which already carries `agent=<name>` and the
 *   window, so /api/agents resolves both the ServiceName filter and the same window the tiles use.
 */
export function AgentDetail({ agent, queryString }: { agent: string; queryString: string }) {
  const endpoint = queryString ? `/api/agents?${queryString}` : "/api/agents";
  const [data, setData] = useState<AgentsDetailPayload | null>(null);
  const [status, setStatus] = useState<FetchStatus>("loading");

  useEffect(() => {
    const ctrl = new AbortController();
    setStatus("loading");
    fetch(endpoint, { signal: ctrl.signal, cache: "no-store" })
      .then((r) => r.json())
      .then((j: AgentsDetailPayload) => {
        setData(j);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        // A fast filter change aborts in-flight; keep the last state rather than flashing a message.
        if (err instanceof Error && err.name === "AbortError") return;
        // Honest-under-uncertainty: a failed fetch is "unavailable", never a zero-filled detail.
        setData(null);
        setStatus("unavailable");
      });
    return () => ctrl.abort();
  }, [endpoint]);

  const title = `Run distribution — ${agent}`;

  if (status === "loading" && data === null) {
    return (
      <Card title={title}>
        <p className="py-6 text-center text-sm text-muted">Loading this agent’s runs…</p>
      </Card>
    );
  }

  if (status === "unavailable" || data === null) {
    return (
      <Card title={title}>
        <p className="py-6 text-center text-sm text-muted">
          This detail is served live and the telemetry source could not be reached, so no
          distribution is drawn.
        </p>
      </Card>
    );
  }

  const summary = data.agents.find((a) => a.name === agent) ?? null;
  const agentRuns = data.runs
    .filter((r) => r.agent === agent)
    .sort((a, b) => b.totalCostMicroUsd - a.totalCostMicroUsd);

  // No summary for the selected agent means it had no runs in this window. State it; never fabricate.
  if (summary === null) {
    return (
      <Card title={title}>
        <p className="py-6 text-center text-sm text-muted">
          No runs for <span className="font-mono">{agent}</span> in this window.
        </p>
      </Card>
    );
  }

  const ratio = p99Ratio(summary);
  const hot = ratio > 20;

  // Reconciler freshness, read exactly as the /agents view did (CTO-169): the real last-run boundary,
  // rendered as `—` (no badge) when the reconciler has never run or its source is unavailable.
  const reconciledThrough = boundaryFromMinutesAgo(data.reconcilerLastRunMinutesAgo);
  const asOf = asOfLabel(reconciledThrough);
  const dataState = deriveDataState({ isEmpty: false, isPartial: false, reconciledThrough });

  return (
    <Card title={title}>
      <div className="space-y-5">
        {asOf && (
          <div className="flex justify-end">
            <StaleBadge
              asOf={asOf}
              age={relativeAge(reconciledThrough)}
              stale={dataState === "stale"}
            />
          </div>
        )}
        <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
          <Stat label="Runs/day" value={summary.runsPerDay.toLocaleString()} />
          <Stat label="Cost/day" value={formatUSD(summary.costPerDayMicroUsd)} />
          <Stat label="p50" value={formatUSD(summary.p50MicroUsd)} />
          <Stat label="p99" value={formatUSD(summary.p99MicroUsd)} />
          <div>
            <div className="text-xs uppercase tracking-wide text-muted">p99/p50</div>
            <div className="mt-0.5 tabular-nums">
              <span className={hot ? "rounded bg-bad/20 px-1.5 py-0.5 text-bad" : ""}>
                {ratio.toFixed(0)}×{hot ? " ⚠" : ""}
              </span>
            </div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-muted">Distribution</div>
            <div className="mt-1">
              <Histogram buckets={summary.distribution} />
            </div>
          </div>
        </div>

        <div>
          <div className="mb-2 text-xs uppercase tracking-wide text-muted">
            Pathological runs (top cost outliers)
          </div>
          {agentRuns.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted">
              No outlier runs captured for this agent in this window.
            </p>
          ) : (
            <ul className="divide-y divide-edge text-sm">
              {agentRuns.map((r) => (
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
          )}
        </div>
      </div>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-0.5 tabular-nums">{value}</div>
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
