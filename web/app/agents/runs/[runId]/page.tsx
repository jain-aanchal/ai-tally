// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import { notFound } from "next/navigation";
import { Card } from "@/components/Card";
// runs imported for generateStaticParams (build-time only); page reads via API at runtime.
import { type AgentRun, runs as runsBuildtime } from "@/lib/agents";
import { apiGet } from "@/lib/api";
import { formatUSD } from "@/lib/types";

export function generateStaticParams() {
  return runsBuildtime.map((r) => ({ runId: r.runId }));
}

export default async function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  let run: AgentRun | null = null;
  try {
    run = await apiGet<AgentRun>(`/api/agents/runs/${encodeURIComponent(runId)}`);
  } catch {
    notFound();
  }
  if (!run) notFound();

  const max = Math.max(...run.spans.map((s) => s.costMicroUsd));

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm text-muted">
        <Link href="/agents" className="text-accent hover:underline">
          Agents
        </Link>
        <span>/</span>
        <span className="font-mono">{run.runId}</span>
      </div>

      <div className="flex items-baseline gap-4">
        <h1 className="text-xl font-semibold">{run.agent}</h1>
        <span className="text-2xl font-semibold tabular-nums">{formatUSD(run.totalCostMicroUsd)}</span>
        <span className="text-bad">{run.multipleOfMedian}× median</span>
        <span className="text-muted">· {run.steps} steps · {run.outcome}</span>
      </div>

      <Card title="Why expensive">
        <p className="text-sm leading-relaxed text-gray-200">{run.whyExpensive}</p>
      </Card>

      <Card title="Run waterfall">
        <div className="space-y-1.5">
          {run.spans.map((s) => {
            const depth = s.parentSpanId ? 1 : 0;
            const pct = max === 0 ? 0 : (s.costMicroUsd / max) * 100;
            return (
              <div key={s.spanId} className="flex items-center gap-3 text-sm">
                <div className="w-64 shrink-0 truncate" style={{ paddingLeft: depth * 16 }}>
                  <span className={s.status === "retry" ? "text-warn" : s.status === "error" ? "text-bad" : ""}>
                    {s.name}
                  </span>
                </div>
                <div className="relative h-4 flex-1 rounded bg-ink">
                  <div
                    className="absolute inset-y-0 left-0 rounded bg-accent/70"
                    style={{ width: `${Math.max(1, pct)}%` }}
                  />
                </div>
                <div className="w-20 shrink-0 text-right tabular-nums">{formatUSD(s.costMicroUsd)}</div>
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}
