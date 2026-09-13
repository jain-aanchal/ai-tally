// SPDX-License-Identifier: Apache-2.0
// The organization's hosted-proxy switch (zero-code connect). Off by default.
//
// Deliberately NOT optimistic, unlike the connector toggle. This controls whether production LLM
// traffic is accepted, so the control shows a new state only after the gateway has stored it.
"use client";

import { useState, useTransition } from "react";

import { Blank } from "@/components/HonestValue";

export function ProxySwitch({
  initialEnabled,
  canManage,
}: {
  /** null when the setting could not be read: shown as unknown, never as "Off". */
  initialEnabled: boolean | null;
  canManage: boolean;
}) {
  const [enabled, setEnabled] = useState(initialEnabled);
  const [status, setStatus] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  function change(next: boolean) {
    setStatus(null);
    startTransition(async () => {
      try {
        const res = await fetch("/api/settings/proxy", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        });
        const body = (await res.json().catch(() => ({}))) as { enabled?: boolean; error?: string };
        if (!res.ok || typeof body.enabled !== "boolean") {
          setStatus({ tone: "err", text: body.error ?? `Could not save (HTTP ${res.status}).` });
          return;
        }
        setEnabled(body.enabled);
        setStatus({
          tone: "ok",
          // Honest about propagation: running proxies learn of it on their next key-feed refresh.
          text: body.enabled
            ? "On. Proxies start accepting this organization's keys within about a minute."
            : "Off. Proxies refuse this organization's keys within about a minute. The SDK is unaffected.",
        });
      } catch (e) {
        setStatus({ tone: "err", text: (e as Error).message });
      }
    });
  }

  return (
    <div className="space-y-2 rounded-md border border-edge bg-panel p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-prose">
          <div className="text-sm font-semibold text-fg">Zero-code proxy</div>
          <p className="mt-1 text-xs text-muted">
            Lets this organization&apos;s keys meter LLM calls by changing a base URL instead of
            installing the SDK. Calls pass through ai-tally&apos;s proxy on the way to the provider;
            prompts and responses are forwarded, never stored. While off, the proxy refuses these
            keys and the SDK keeps working.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {enabled === null ? (
            <span className="text-sm text-muted">
              <Blank reason="the setting could not be read from the gateway, so whether the proxy is on is unknown" />
            </span>
          ) : (
            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${
                enabled ? "border-good/40 bg-good/10 text-good" : "border-edge bg-ink/40 text-muted"
              }`}
            >
              <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${enabled ? "bg-good" : "bg-muted"}`} />
              {enabled ? "On" : "Off"}
            </span>
          )}
          {canManage && enabled !== null && (
            <button
              type="button"
              disabled={pending}
              onClick={() => change(!enabled)}
              className="rounded border border-edge px-2.5 py-1 text-xs font-medium text-fg disabled:opacity-50"
            >
              {pending ? "Saving…" : enabled ? "Turn off" : "Turn on"}
            </button>
          )}
        </div>
      </div>
      {!canManage && (
        <p className="text-xs text-muted">Only an organization admin can change this.</p>
      )}
      {status && (
        <p className={`text-xs ${status.tone === "ok" ? "text-good" : "text-warn"}`}>{status.text}</p>
      )}
    </div>
  );
}
