// SPDX-License-Identifier: Apache-2.0
// First-data onboarding surface (Initiative 2, §9), shown right after a key is minted. Two halves:
//
//   1. A per-path, per-language snippet generator with the REAL key inlined. The key comes from the
//      one-time mint response and is never stored: this component only renders while the parent still
//      holds that response, so nothing has to be re-fetched to show it later.
//   2. A live "we received your first event" indicator that polls the tenant-scoped ClickHouse probe
//      (/api/onboarding/first-event) and flips from waiting to connected when the first span lands. It
//      reports the honest state only (connected / waiting / unknown), never a fabricated success.
"use client";

import { useEffect, useState } from "react";

import { connectSnippets, type ConnectPath, type Snippet } from "@/lib/connectSnippets";
import type { FirstEventPayload } from "@/app/api/onboarding/first-event/route";
import { useLivePoll } from "@/lib/useLivePoll";

const PATH_LABELS: Record<ConnectPath, string> = {
  proxy: "Proxy (zero-code)",
  sdk: "SDK (deep context)",
};

function CodeBlock({ snippet }: { snippet: Snippet }) {
  // "idle" until a copy actually resolves. We report "Copied" ONLY on a real success and "Copy failed"
  // when the Clipboard API is missing (insecure context / older browser) or writeText() rejects
  // (permission denied), never an unconditional success (Initiative 2 §9 review).
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  async function copy() {
    try {
      const clipboard = navigator.clipboard;
      if (!clipboard?.writeText) throw new Error("clipboard unavailable");
      await clipboard.writeText(snippet.code);
      setState("copied");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState("idle"), 1500);
  }

  const label = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wider text-muted">{snippet.language}</span>
        <button
          type="button"
          onClick={() => {
            void copy();
          }}
          className="rounded border border-edge px-2 py-0.5 text-xs text-fg"
        >
          {label}
        </button>
      </div>
      <pre className="overflow-x-auto rounded border border-edge bg-transparent p-3 text-xs text-fg">
        <code>{snippet.code}</code>
      </pre>
      {snippet.note && <p className="text-xs text-muted">{snippet.note}</p>}
    </div>
  );
}

function FirstEventBadge() {
  // Poll the tenant-scoped probe. Start from "unknown" so the first paint makes no claim before the
  // first fetch resolves.
  //
  // The first event only ever arrives ONCE, so once we see "connected" we stop polling: continuing
  // to hit the ClickHouse probe every 4s forever is pure waste (Initiative 2 §9 review). "connected"
  // is terminal, so `done` latches and the interval never restarts, even if a later reset were to
  // momentarily blank the data.
  const [done, setDone] = useState(false);
  const { data } = useLivePoll<FirstEventPayload>(
    "/api/onboarding/first-event",
    { status: "unknown" },
    { intervalMs: 4000, enabled: !done },
  );
  const status = data.status;
  useEffect(() => {
    if (status === "connected") setDone(true);
  }, [status]);
  if (status === "connected") {
    return (
      <div className="rounded-md border border-accent bg-panel px-3 py-2 text-sm text-fg">
        <span className="font-semibold">We received your first event.</span>{" "}
        <a className="underline" href="/explore">
          View Cost Explorer
        </a>
      </div>
    );
  }
  if (status === "waiting") {
    return (
      <div className="rounded-md border border-edge bg-panel px-3 py-2 text-sm text-muted">
        Waiting for your first event. Run the snippet above and it appears here.
      </div>
    );
  }
  // unknown: the probe could not reach ClickHouse. Honest, not a fabricated waiting/connected.
  return (
    <div className="rounded-md border border-edge bg-panel px-3 py-2 text-sm text-muted">
      Checking for your first event…
    </div>
  );
}

export function ConnectPanel({ token }: { token: string }) {
  const [path, setPath] = useState<ConnectPath>("proxy");
  const snippets = connectSnippets(token);
  const [active, setActive] = useState(0);
  const current = snippets[path];
  const shown = current[Math.min(active, current.length - 1)];

  return (
    <div className="space-y-3 rounded-md border border-edge bg-panel p-4">
      <div className="text-sm font-semibold text-fg">Connect your app</div>
      <div className="flex gap-2">
        {(Object.keys(PATH_LABELS) as ConnectPath[]).map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => {
              setPath(p);
              setActive(0);
            }}
            className={
              "rounded px-2 py-1 text-xs " +
              (path === p ? "bg-accent text-white" : "border border-edge text-fg")
            }
          >
            {PATH_LABELS[p]}
          </button>
        ))}
      </div>
      {current.length > 1 && (
        <div className="flex gap-2">
          {current.map((s, i) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setActive(i)}
              className={
                "rounded px-2 py-0.5 text-xs " +
                (i === active ? "bg-fg text-panel" : "border border-edge text-muted")
              }
            >
              {s.label}
            </button>
          ))}
        </div>
      )}
      <CodeBlock snippet={shown} />
      <FirstEventBadge />
    </div>
  );
}
