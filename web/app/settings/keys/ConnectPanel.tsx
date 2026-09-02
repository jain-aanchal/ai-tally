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

import { useState } from "react";

import { connectSnippets, type ConnectPath, type Snippet } from "@/lib/connectSnippets";
import type { FirstEventPayload } from "@/app/api/onboarding/first-event/route";
import { useLivePoll } from "@/lib/useLivePoll";

const PATH_LABELS: Record<ConnectPath, string> = {
  proxy: "Proxy (zero-code)",
  sdk: "SDK (deep context)",
};

function CodeBlock({ snippet }: { snippet: Snippet }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wider text-muted">{snippet.language}</span>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(snippet.code);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
          className="rounded border border-edge px-2 py-0.5 text-xs text-fg"
        >
          {copied ? "Copied" : "Copy"}
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
  const { data } = useLivePoll<FirstEventPayload>(
    "/api/onboarding/first-event",
    { status: "unknown" },
    { intervalMs: 4000 },
  );
  const status = data.status;
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
