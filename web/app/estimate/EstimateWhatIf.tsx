// SPDX-License-Identifier: Apache-2.0
"use client";

import { useState, type ReactNode } from "react";
import { Card } from "@/components/Card";
import { Diag } from "@/components/Diag";
import { Blank, Money } from "@/components/HonestValue";
import { pctDelta, type Projection, type WhatIfProjection } from "@/lib/estimate";

// A small hardcoded candidate list keeps the picker simple (CTO-128 out-of-scope: model catalog).
const CANDIDATES = [
  { provider: "anthropic", model: "claude-haiku-4-5", label: "Claude Haiku 4.5" },
  { provider: "anthropic", model: "claude-sonnet-4.5", label: "Claude Sonnet 4.5" },
  { provider: "openai", model: "gpt-5-mini", label: "GPT-5 mini" },
  { provider: "openai", model: "gpt-4o-mini", label: "GPT-4o mini" },
];

type Proposed = WhatIfProjection["proposed"];

// CTO-298: the reasons behind the blanks on this page. Each says what is missing and why, because a
// blank whose reason a reader cannot find reads as a bug rather than as an honest unknown.
const NO_BASELINE = "no priced baseline traffic for this workload yet";
const NO_COST_DISTRIBUTION =
  "p99 cost per run needs a full cost distribution; the replay executor returns per-call totals only, so no percentile is available yet";
const NO_LATENCY_DISTRIBUTION =
  "mean latency needs a full latency distribution; replay returns a p50 only, and a median is not a mean";
const NO_RISK =
  "blow-up risk is the probability that p99 cost more than doubles, which needs the p99 distribution replay does not return yet";

function Latency({ ms, reason }: { ms: number | null; reason: string }) {
  return ms === null ? <Blank reason={reason} /> : <>{ms} ms</>;
}

export function EstimateWhatIf({ initial }: { initial: Projection }) {
  const { current } = initial;

  const [model, setModel] = useState(CANDIDATES[0].model);
  const [systemPromptOverride, setSystemPromptOverride] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Start from whatever the GET returned; POST replaces them.
  const [proposed, setProposed] = useState<Proposed>(initial.proposed);
  const [sampleUsed, setSampleUsed] = useState(initial.sample.used);
  const [grounded, setGrounded] = useState<number | null>(null);

  async function onEstimate() {
    setPending(true);
    setError(null);
    try {
      const provider = CANDIDATES.find((c) => c.model === model)?.provider ?? "anthropic";
      const res = await fetch("/api/estimate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          candidateModel: model,
          providerOverride: provider,
          systemPromptOverride: systemPromptOverride.trim() || undefined,
        }),
      });
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { error?: string };
        throw new Error(j.error ?? `request failed (${res.status})`);
      }
      const body = (await res.json()) as WhatIfProjection;
      setProposed(body.proposed);
      setSampleUsed(body.sample.used);
      setGrounded(body.groundedSamples);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  }

  const costDelta = pctDelta(current.monthlyCostMicroUsd, proposed.monthlyCostMicroUsd);
  const p99Delta = pctDelta(current.p99CostMicroUsd, proposed.p99CostMicroUsd);
  const latDelta = pctDelta(current.meanLatencyMs, proposed.meanLatencyMs);
  const riskSeverity =
    initial.blowUpRisk === null
      ? undefined
      : initial.blowUpRisk >= 0.3
        ? "bad"
        : initial.blowUpRisk >= 0.1
          ? "warn"
          : "good";

  return (
    <div className="space-y-6">
      <Card title="What-if">
        <div className="space-y-3 text-sm">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="space-y-1">
              <span className="text-xs uppercase tracking-wide text-muted">Candidate model</span>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="w-full rounded-lg border border-edge bg-panel px-3 py-2"
              >
                {CANDIDATES.map((c) => (
                  <option key={`${c.provider}/${c.model}`} value={c.model}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="block space-y-1">
            <span className="text-xs uppercase tracking-wide text-muted">
              System prompt override (optional)
            </span>
            <textarea
              value={systemPromptOverride}
              onChange={(e) => setSystemPromptOverride(e.target.value)}
              rows={4}
              placeholder="Leave blank to replay the captured prompt as-is."
              className="w-full rounded-lg border border-edge bg-panel px-3 py-2 font-mono text-xs"
            />
          </label>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={onEstimate}
              disabled={pending}
              className="rounded-lg border border-accent/40 bg-accent/10 px-4 py-2 font-medium text-accent disabled:opacity-50"
            >
              {pending ? "Estimating…" : "Estimate"}
            </button>
            {error && <span className="text-bad">{error}</span>}
            {grounded !== null && !error && (
              <span className="text-muted">
                grounded on {grounded} replayed sample{grounded === 1 ? "" : "s"}
                {proposed.monthlyCostMicroUsd === null && " (too few to report a cost)"}
              </span>
            )}
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          label="Cost / month"
          current={<Money micro={current.monthlyCostMicroUsd} reason={NO_BASELINE} />}
          proposed={
            <Money micro={proposed.monthlyCostMicroUsd} reason="no replayed samples ground a projected cost yet" />
          }
          delta={costDelta}
          betterWhenNegative
        />
        <Kpi
          label="p99 cost / run"
          current={<Money micro={current.p99CostMicroUsd} reason={NO_COST_DISTRIBUTION} />}
          proposed={<Money micro={proposed.p99CostMicroUsd} reason={NO_COST_DISTRIBUTION} />}
          delta={p99Delta}
          betterWhenNegative
          headline
        />
        <Kpi
          label="Blow-up risk"
          current={<Blank reason="the current model is the baseline; there is no risk of it changing relative to itself" />}
          proposed={
            initial.blowUpRisk === null ? (
              <Blank reason={NO_RISK} />
            ) : (
              `${Math.round(initial.blowUpRisk * 100)}%`
            )
          }
          severity={riskSeverity}
          hint="P(p99 > 2× current)"
        />
        <Kpi
          label="Mean latency"
          current={<Latency ms={current.meanLatencyMs} reason={NO_LATENCY_DISTRIBUTION} />}
          proposed={<Latency ms={proposed.meanLatencyMs} reason={NO_LATENCY_DISTRIBUTION} />}
          delta={latDelta}
          betterWhenNegative
        />
      </div>

      <Card title="Driver breakdown">
        {initial.drivers.length === 0 ? (
          // CTO-298: no drivers is a real answer, and it is not a breakdown that sums to zero. The
          // fixture's three (a longer system prompt, a new tool call, cached input recapture) used
          // to render here for every tenant.
          <p className="text-sm text-muted">
            No cost drivers attributed yet. Drivers are computed from a replayed corpus, and nothing
            has been replayed for this workload.
          </p>
        ) : (
          <ul className="space-y-2 text-sm">
            {initial.drivers.map((d) => (
              <li key={d.reason} className="flex items-baseline justify-between gap-3">
                <span className="text-muted">{d.reason}</span>
                <span
                  className={`tabular-nums font-medium ${d.delta > 0 ? "text-bad" : "text-good"}`}
                >
                  {d.delta > 0 ? "+" : ""}
                  <Money micro={d.delta} />
                  /mo
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Sample diagnostics">
        <dl className="grid grid-cols-1 gap-y-1.5 text-sm sm:grid-cols-2">
          <Diag k="samples used" v={`${sampleUsed}`} />
          <Diag
            k="pathological runs included"
            v={
              initial.sample.pathologicalIncluded === null ? (
                <Blank reason="no corpus has been sampled, so no pathological runs were selected or excluded" />
              ) : (
                initial.sample.pathologicalIncluded.toString()
              )
            }
            good={initial.sample.pathologicalIncluded !== null}
          />
          <Diag
            k="confidence interval (on p99)"
            v={
              initial.sample.ciHalfWidthPct === null ? (
                <Blank reason={NO_COST_DISTRIBUTION} />
              ) : (
                `±${Math.round(initial.sample.ciHalfWidthPct * 100)}%`
              )
            }
          />
          <Diag
            k="sampling strategy"
            v={
              initial.sample.tailWeighted === null ? (
                <Blank reason="no sampling strategy has run for this workload yet" />
              ) : (
                "tail-weighted (recommended)"
              )
            }
            good={initial.sample.tailWeighted !== null}
          />
        </dl>
      </Card>
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
  current: ReactNode;
  proposed: ReactNode;
  delta?: number | null;
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
      {delta !== undefined && delta !== null && delta !== 0 && (
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
