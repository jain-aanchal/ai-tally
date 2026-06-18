// SPDX-License-Identifier: Apache-2.0
"use client";

// The Stripe tile (CTO-110). A small client component because the "Connect" flow needs an input
// field for pasting the signing secret — everything else on /connectors is a server component.
//
// States: Not connected → Connecting (show webhook URL + paste box) → Connected (fingerprint +
// connected_at). The fingerprint replaces the secret so a tenant can confirm "yes, this is the
// right key" without us ever round-tripping the raw value.

import { useState, useTransition } from "react";

import { connectStripeAction } from "./stripeActions";
import type { StripeConfigView } from "@/lib/stripeConnector";

interface Props {
  initialConfig: StripeConfigView | null;
  webhookUrl: string;
}

export function StripeTile({ initialConfig, webhookUrl }: Props) {
  const [config, setConfig] = useState<StripeConfigView | null>(initialConfig);
  const [mode, setMode] = useState<"idle" | "connecting">(
    initialConfig?.isActive ? "idle" : "connecting",
  );
  const [secret, setSecret] = useState("");
  const [accountId, setAccountId] = useState("");
  const [status, setStatus] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  const onConnect = () => {
    setStatus(null);
    if (!secret.trim()) {
      setStatus({ tone: "err", text: "Paste the signing secret from Stripe Dashboard." });
      return;
    }
    startTransition(async () => {
      const res = await connectStripeAction(secret.trim(), accountId.trim() || null);
      if (res.ok) {
        setStatus({ tone: "ok", text: "Connected. Events will start flowing on the next webhook delivery." });
        setSecret("");
        setConfig({
          secretFingerprint: res.fingerprint ?? null,
          stripeAccountId: accountId.trim() || null,
          connectedAt: new Date().toISOString(),
          disconnectedAt: null,
          isActive: true,
        });
        setMode("idle");
      } else {
        setStatus({ tone: "err", text: res.error ?? "Failed to connect Stripe." });
      }
    });
  };

  const isConnected = config?.isActive ?? false;

  return (
    <div className="rounded-lg border border-edge bg-ink/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold">Stripe revenue</h3>
            {isConnected ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-good/40 bg-good/10 px-2 py-0.5 text-xs font-medium text-good">
                <span className="h-1.5 w-1.5 rounded-full bg-good" />
                Connected
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-ink/40 px-2 py-0.5 text-xs font-medium text-muted">
                <span className="h-1.5 w-1.5 rounded-full bg-muted" />
                Not connected
              </span>
            )}
          </div>
          <p className="mt-1 max-w-prose text-xs text-muted">
            Verified webhook ingest for checkout.session.completed, invoice.paid, charge.refunded,
            and customer.subscription.deleted. Once connected, Value/user and Margin/user light up
            on the attribution view.
          </p>
        </div>
        {isConnected && (
          <button
            type="button"
            onClick={() => setMode("connecting")}
            className="rounded border border-edge bg-ink/40 px-2.5 py-1 text-xs font-medium text-muted hover:bg-ink/60"
          >
            Rotate secret
          </button>
        )}
      </div>

      {isConnected && config && (
        <dl className="mt-3 grid grid-cols-2 gap-y-1 text-xs sm:grid-cols-3">
          <dt className="text-muted">Secret fingerprint</dt>
          <dd className="font-mono">{config.secretFingerprint ?? "—"}</dd>
          <dt className="text-muted">Stripe account</dt>
          <dd className="font-mono">{config.stripeAccountId ?? "—"}</dd>
          <dt className="text-muted">Connected at</dt>
          <dd className="tabular-nums">
            {config.connectedAt ? new Date(config.connectedAt).toLocaleString() : "—"}
          </dd>
        </dl>
      )}

      {mode === "connecting" && (
        <div className="mt-3 space-y-2 rounded border border-edge bg-ink/40 p-3 text-xs">
          <p className="text-muted">
            In your Stripe Dashboard → Developers → Webhooks, add an endpoint pointing to:
          </p>
          <code className="block break-all rounded bg-ink/60 px-2 py-1 font-mono text-[11px]">
            {webhookUrl}
          </code>
          <p className="text-muted">
            Select the four events above, then paste the signing secret (<span className="font-mono">whsec_…</span>) Stripe shows you:
          </p>
          <input
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            placeholder="whsec_..."
            className="w-full rounded border border-edge bg-ink/60 px-2 py-1 font-mono text-xs"
            autoComplete="off"
          />
          <input
            type="text"
            value={accountId}
            onChange={(e) => setAccountId(e.target.value)}
            placeholder="Stripe account id (optional, acct_...)"
            className="w-full rounded border border-edge bg-ink/60 px-2 py-1 font-mono text-xs"
          />
          <p className="text-muted">
            For local testing, run{" "}
            <code className="rounded bg-ink/60 px-1 py-0.5 font-mono text-[11px]">
              stripe listen --forward-to {webhookUrl}
            </code>{" "}
            and copy the printed whsec from the CLI.
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onConnect}
              disabled={pending}
              className="rounded bg-accent px-3 py-1 text-xs font-medium text-ink hover:bg-accent/90 disabled:opacity-50"
            >
              {pending ? "Saving…" : isConnected ? "Rotate" : "Connect"}
            </button>
            {isConnected && (
              <button
                type="button"
                onClick={() => {
                  setMode("idle");
                  setSecret("");
                  setStatus(null);
                }}
                className="text-muted hover:underline"
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      )}

      {status && (
        <p className={`mt-2 text-xs ${status.tone === "ok" ? "text-good" : "text-warn"}`}>
          {status.text}
        </p>
      )}
    </div>
  );
}
