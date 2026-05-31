"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  type FunnelStage,
  type OnboardingProgress,
  type TenantProxyCredentials,
  activationStatus,
  deriveChecklist,
  formatDuration,
  proxyEnvSnippet,
  proxyPythonSnippet,
} from "@/lib/onboarding";

const POLL_MS = 2000;

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
}: {
  initialProgress: OnboardingProgress;
  creds: TenantProxyCredentials;
}) {
  const [progress, setProgress] = useState<OnboardingProgress>(initialProgress);
  const [tab, setTab] = useState<"env" | "python">("env");
  const [copied, setCopied] = useState(false);

  const status = activationStatus(progress);
  const steps = deriveChecklist(progress);

  // Live "waiting for first trace" detector: poll until a trace is observed, then stop.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (progress.firstTraceAt !== null) return;
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch("/api/onboarding/first-trace", { cache: "no-store" });
        const data = (await res.json()) as { received: boolean; firstTraceAt: number | null };
        if (data.received && data.firstTraceAt !== null) {
          setProgress((p) => ({ ...p, firstTraceAt: data.firstTraceAt }));
        }
      } catch {
        /* keep polling */
      }
    }, POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [progress.firstTraceAt]);

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

  const onSendTestTrace = useCallback(async () => {
    try {
      const res = await fetch("/api/onboarding/first-trace", { method: "POST" });
      const data = (await res.json()) as { firstTraceAt: number | null };
      if (data.firstTraceAt !== null) {
        setProgress((p) => ({ ...p, firstTraceAt: data.firstTraceAt }));
      }
    } catch {
      /* ignore */
    }
  }, []);

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]">
      <div className="space-y-6">
        {/* Step 1 — proxy config */}
        <section className="rounded-xl border border-edge bg-panel p-5">
          <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-muted">
            1 · Point your app at the proxy
          </h2>
          <p className="mb-3 text-sm text-muted">
            Set two environment variables. Your <code className="text-gray-300">OPENAI_API_KEY</code>{" "}
            stays in your environment — we never see it.
          </p>

          <div className="mb-2 flex gap-1 text-xs">
            <TabButton active={tab === "env"} onClick={() => setTab("env")}>
              Shell / env
            </TabButton>
            <TabButton active={tab === "python"} onClick={() => setTab("python")}>
              Python
            </TabButton>
          </div>

          <div className="relative">
            <pre className="overflow-x-auto rounded-lg border border-edge bg-ink p-3 font-mono text-xs leading-relaxed text-gray-200">
              {snippet}
            </pre>
            <button
              type="button"
              onClick={onCopy}
              className="absolute right-2 top-2 rounded-md border border-edge bg-panel px-2 py-1 text-xs text-gray-300 hover:text-accent"
            >
              {copied ? "Copied ✓" : "Copy"}
            </button>
          </div>
        </section>

        {/* Step 2 — live first-trace detector */}
        <section className="rounded-xl border border-edge bg-panel p-5">
          <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-muted">
            2 · Send your first request
          </h2>
          <p className="mb-3 text-sm text-muted">
            Make any OpenAI call from your app. We detect the first trace automatically.
          </p>

          {progress.firstTraceAt === null ? (
            <div className="flex items-center justify-between gap-4 rounded-lg border border-edge bg-ink p-4">
              <span className="flex items-center gap-2 text-sm text-gray-200">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
                Waiting for your first trace…
              </span>
              <button
                type="button"
                onClick={onSendTestTrace}
                className="rounded-md border border-edge px-3 py-1.5 text-xs text-muted hover:text-accent"
              >
                Send a test trace
              </button>
            </div>
          ) : (
            <div className="rounded-lg border border-good/40 bg-good/10 p-4 text-sm text-good">
              First trace received{" "}
              {status.timeToFirstTraceMs !== null && (
                <>
                  in <strong>{formatDuration(status.timeToFirstTraceMs)}</strong>
                  {status.withinTarget ? " — under the 5-minute target ✓" : ""}
                </>
              )}
              . Your dashboards will populate as traces flow in.
            </div>
          )}
        </section>
      </div>

      {/* Right rail — activation checklist */}
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
                    s.done
                      ? "border-good bg-good/20 text-good"
                      : "border-edge text-muted"
                  }`}
                >
                  {s.done ? "✓" : ""}
                </span>
                <div>
                  <div className={`text-sm ${s.done ? "text-gray-200" : "text-gray-300"}`}>
                    {s.title}
                  </div>
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
        active ? "bg-edge text-white" : "text-muted hover:text-gray-200"
      }`}
    >
      {children}
    </button>
  );
}
