// SPDX-License-Identifier: Apache-2.0
// Onboarding connect steps + activation checklist.
//
// #320: step 2 and the coverage panel used to disagree in public. Step 2 read `firstTraceAt` out of
// the client-side onboarding store while the panel three inches below it read the gateway's
// coverage probe, so a stack with spans in it rendered "Waiting for your first trace…" directly
// above "LLM calls · Flowing · 12 spans". One fact, two sources, and the page argued with itself.
//
// Both now read the SAME poll. The coverage probe is the only thing that can prove a trace arrived
// (a `covered` layer is unreachable without a proving span), so this component owns the poll,
// derives step 2's state from it, and hands the result to the panel as its rendered answer. There
// is no second source left to drift, and the probe's third state travels with it: a probe we could
// not read leaves step 2 saying so, rather than fabricating a definite "no trace yet".
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { COVERAGE_POLL_MS, CoveragePanel } from "@/components/CoveragePanel";
import { Blank } from "@/components/HonestValue";
import { type LayerCoverage, unknownCoverage } from "@/lib/firstEvent";
import {
  EXAMPLE_CREDS_NOTICE,
  type FunnelStage,
  type OnboardingProgress,
  type TenantProxyCredentials,
  activationStatus,
  deriveChecklist,
  formatDuration,
  proxyEnvSnippet,
  proxyPythonSnippet,
  spanCountLabel,
  traceEvidenceFromCoverage,
} from "@/lib/onboarding";

const PROBE_SILENT =
  "the coverage probe has not answered yet, so we cannot tell whether a trace has arrived";

async function postFunnel(stage: FunnelStage): Promise<void> {
  try {
    await fetch("/api/onboarding", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ stage }),
    });
  } catch {
    /* funnel tracking is best-effort */
  }
}

export function Onboarding({
  initialProgress,
  creds,
  initialLayers,
}: {
  initialProgress: OnboardingProgress;
  creds: TenantProxyCredentials;
  /** Seed for the shared coverage poll. Omitted in the app; supplied by tests. */
  initialLayers?: LayerCoverage[];
}) {
  const [progress, setProgress] = useState<OnboardingProgress>(initialProgress);
  const [tab, setTab] = useState<"env" | "python">("env");
  const [copied, setCopied] = useState(false);
  const [layers, setLayers] = useState<LayerCoverage[]>(
    initialLayers ?? unknownCoverage(PROBE_SILENT),
  );

  // The single source of truth for "has a trace arrived", shared with the panel below.
  const evidence = useMemo(() => traceEvidenceFromCoverage(layers), [layers]);

  // The right rail reads the same evidence as step 2, so the checklist cannot disagree with the
  // panel either. It ticks the step without inventing a timestamp for it: the probe proves a trace
  // arrived, not WHEN it arrived, so time-to-first-trace stays null unless the funnel timed the
  // arrival itself. Back-filling Date.now() here would have turned "we noticed spans just now" into
  // a 5-minute-target measurement nobody took (CLAUDE.md, honest under uncertainty).
  const probeEvidence = { firstTraceProven: evidence.state === "received" };
  const status = activationStatus(progress, probeEvidence);
  const steps = deriveChecklist(progress, probeEvidence);

  // One poll, two readers. Keeps the last honest answer on a failed request: a dropped fetch is not
  // evidence that instrumentation stopped, same rule the panel already applied to its own poll.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch("/api/onboarding/coverage", { cache: "no-store" });
        const data = (await res.json()) as { layers?: LayerCoverage[] };
        if (!cancelled && Array.isArray(data.layers) && data.layers.length) {
          setLayers(data.layers);
        }
      } catch {
        /* keep the last answer and try again */
      }
    };
    void tick();
    const id = setInterval(tick, COVERAGE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const snippet = tab === "env" ? proxyEnvSnippet(creds) : proxyPythonSnippet(creds);

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(snippet);
    } catch {
      /* clipboard may be unavailable; still record intent */
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    if (progress.copiedConfigAt === null) {
      setProgress((p) => ({ ...p, copiedConfigAt: Date.now() }));
      void postFunnel("copied_config");
    }
  }, [snippet, progress.copiedConfigAt]);

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]">
        <div className="space-y-6">
          {/* Step 1: proxy config */}
          <section className="rounded-xl border border-edge bg-panel p-5">
            <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-muted">
              1 · Point your app at the proxy
            </h2>
            <p className="mb-3 text-sm text-muted">
              Set two environment variables. Your <code className="text-muted">OPENAI_API_KEY</code>{" "}
              stays in your environment. We never see it.
            </p>

            {/* #320: the key and URL below are placeholders until provisioning lands, and a
                placeholder shown in the same block a real credential would occupy reads as real.
                Say so, unmissably, above the block rather than only in a comment. */}
            {creds.isExample && (
              <p
                data-testid="example-creds-notice"
                className="mb-3 rounded-lg border border-warn/40 bg-warn/10 p-3 text-xs text-warn"
              >
                <strong className="uppercase tracking-wide">Example values.</strong> {EXAMPLE_CREDS_NOTICE}
              </p>
            )}

            <div className="mb-2 flex gap-1 text-xs">
              <TabButton active={tab === "env"} onClick={() => setTab("env")}>
                Shell / env
              </TabButton>
              <TabButton active={tab === "python"} onClick={() => setTab("python")}>
                Python
              </TabButton>
            </div>

            <div className="relative">
              <pre className="overflow-x-auto rounded-lg border border-edge bg-ink p-3 font-mono text-xs leading-relaxed text-fg">
                {snippet}
              </pre>
              <button
                type="button"
                onClick={onCopy}
                className="absolute right-2 top-2 rounded-md border border-edge bg-panel px-2 py-1 text-xs text-muted hover:text-accent"
              >
                {copied ? "Copied ✓" : "Copy"}
              </button>
            </div>
          </section>

          {/* Step 2: first-trace state, read from the same probe the panel below renders */}
          <section className="rounded-xl border border-edge bg-panel p-5">
            <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-muted">
              2 · Send your first request
            </h2>
            <p className="mb-3 text-sm text-muted">
              Make any OpenAI call from your app. We detect the first trace from the same
              instrumentation probe the coverage panel below reports on.
            </p>

            {evidence.state === "received" ? (
              <div className="rounded-lg border border-good/40 bg-good/10 p-4 text-sm text-good">
                First trace received
                {status.timeToFirstTraceMs !== null && (
                  <>
                    {" "}
                    in <strong>{formatDuration(status.timeToFirstTraceMs)}</strong>
                    {status.withinTarget ? ", under the 5-minute target ✓" : ""}
                  </>
                )}
                .{" "}
                {evidence.provingSpans !== null && (
                  <>
                    <strong>{spanCountLabel(evidence.provingSpans)}</strong> already prove it.{" "}
                  </>
                )}
                Your dashboards will populate as traces flow in.
              </div>
            ) : evidence.state === "waiting" ? (
              <div className="flex items-center gap-2 rounded-lg border border-edge bg-ink p-4 text-sm text-fg">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
                Waiting for your first trace…
              </div>
            ) : (
              // The probe could not be read. "No trace yet" would be a definite negative invented
              // out of our own outage, so step 2 renders the blank with the reason instead.
              <div className="flex items-center gap-2 rounded-lg border border-edge bg-ink p-4 text-sm text-muted">
                <Blank reason={evidence.reason} /> We cannot tell whether a trace has arrived yet.
              </div>
            )}
          </section>
        </div>

        {/* Right rail: the activation checklist */}
        <aside className="space-y-3">
          <section className="rounded-xl border border-edge bg-panel p-4">
            <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-muted">
              Getting started
            </h2>
            <ol className="space-y-3">
              {steps.map((s) => (
                <li key={s.id} className="flex gap-3">
                  <span
                    className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-xs ${
                      s.done ? "border-good bg-good/20 text-good" : "border-edge text-muted"
                    }`}
                  >
                    {s.done ? "✓" : ""}
                  </span>
                  <div>
                    <div className={`text-sm ${s.done ? "text-fg" : "text-muted"}`}>{s.title}</div>
                    <div className="text-xs text-muted">{s.hint}</div>
                  </div>
                </li>
              ))}
            </ol>
            <div className="mt-4 border-t border-edge pt-3 text-xs text-muted">
              {status.completedSteps}/{status.totalSteps} complete
            </div>
          </section>
        </aside>
      </div>

      {/* Per-layer coverage (CTO-261, §4.1). Sits under the connect steps because it answers the
          question that comes NEXT: the steps above get the LLM layer flowing, and this says which
          of the remaining layers a span actually proves. `poll={false}` because the poll now lives
          one level up and feeds step 2 as well (#320); the panel renders the answer it is given. */}
      <CoveragePanel initialLayers={layers} poll={false} />
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-2 py-1 ${
        active ? "bg-edge text-fg" : "text-muted hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}
