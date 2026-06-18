// SPDX-License-Identifier: Apache-2.0
"use client";

// Third-party integration cards (CTO-117). Renders three honest states per integration:
//
//   - "healthy"   — green dot, real relativeAge(last_run_at), real event count
//   - "failing"   — yellow dot, last_run_at, truncated error preview, "View errors" button
//                   that flips to the full message (no modal, just a reveal — keep it simple)
//   - "not-connected" — gray, "Connect" CTA, no fabricated stats
//
// Client component because the "View errors" reveal is interactive. The rest of /connectors
// stays server-rendered.

import { useState } from "react";

import { relativeAge } from "@/lib/dataState";
import {
  type IntegrationCardView,
  truncateError,
} from "@/lib/integrations";

interface Props {
  cards: IntegrationCardView[];
}

export function IntegrationCards({ cards }: Props) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {cards.map((c) => (
        <IntegrationCard key={c.def.id} card={c} />
      ))}
    </div>
  );
}

function StateDot({ state }: { state: IntegrationCardView["state"] }) {
  if (state === "healthy") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-good/40 bg-good/10 px-2 py-0.5 text-xs font-medium text-good">
        <span className="h-1.5 w-1.5 rounded-full bg-good" />
        Connected
      </span>
    );
  }
  if (state === "failing") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-warn/40 bg-warn/10 px-2 py-0.5 text-xs font-medium text-warn">
        <span className="h-1.5 w-1.5 rounded-full bg-warn" />
        Failing
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-ink/40 px-2 py-0.5 text-xs font-medium text-muted">
      <span className="h-1.5 w-1.5 rounded-full bg-muted" />
      Not connected
    </span>
  );
}

function IntegrationCard({ card }: { card: IntegrationCardView }) {
  const [showFullError, setShowFullError] = useState(false);
  const { def, state, row } = card;

  return (
    <div className="rounded border border-edge bg-ink/20 p-3" data-testid={`integration-card-${def.id}`}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium">{def.name}</div>
          <div className="mt-0.5 max-w-prose text-xs text-muted">{def.blurb}</div>
        </div>
        <StateDot state={state} />
      </div>

      {state === "healthy" && row && (
        <dl className="mt-3 grid grid-cols-2 gap-y-1 text-xs">
          <dt className="text-muted">Last sync</dt>
          <dd className="text-right tabular-nums">{relativeAge(row.last_run_at)}</dd>
          <dt className="text-muted">Events (24h)</dt>
          <dd className="text-right tabular-nums">{row.total_events_24h.toLocaleString()}</dd>
          <dt className="text-muted">Events (7d)</dt>
          <dd className="text-right tabular-nums">{row.total_events_7d.toLocaleString()}</dd>
        </dl>
      )}

      {state === "failing" && row && (
        <div className="mt-3 space-y-2 text-xs">
          <div className="flex justify-between">
            <span className="text-muted">Last successful sync</span>
            <span className="tabular-nums">{relativeAge(row.last_run_at)}</span>
          </div>
          {row.last_run_error_message && (
            <div className="rounded border border-warn/30 bg-warn/5 p-2">
              <div className="font-mono text-xs text-warn break-words">
                {showFullError
                  ? row.last_run_error_message
                  : truncateError(row.last_run_error_message)}
              </div>
              {row.last_run_error_message.length > 80 && (
                <button
                  type="button"
                  onClick={() => setShowFullError((v) => !v)}
                  className="mt-1 text-xs text-warn underline hover:opacity-80"
                >
                  {showFullError ? "Hide details" : "View errors"}
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {state === "not-connected" && (
        <div className="mt-3">
          <a
            href={def.setupHref}
            target={def.setupHref.startsWith("http") ? "_blank" : undefined}
            rel={def.setupHref.startsWith("http") ? "noreferrer" : undefined}
            className="inline-flex items-center rounded border border-edge bg-ink/40 px-2 py-1 text-xs text-muted hover:text-fg"
          >
            Connect →
          </a>
        </div>
      )}
    </div>
  );
}
