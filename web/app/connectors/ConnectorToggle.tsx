// SPDX-License-Identifier: Apache-2.0
"use client";

// Client toggle for one cost-layer connector (CTO-107). Posts to the gateway via a server action,
// then shows an inline confirmation. Disabled layers stop contributing to the "Partial data" banner
// on /cost and /home — that's the whole behavior change this ticket ships.
import { useState, useTransition } from "react";

import { toggleConnectorAction } from "./actions";

interface Props {
  layer: string;
  /** Initial enabled state from the gateway, used to seed the toggle on first render. */
  initialEnabled: boolean;
}

export function ConnectorToggle({ layer, initialEnabled }: Props) {
  const [enabled, setEnabled] = useState(initialEnabled);
  const [status, setStatus] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  const onClick = () => {
    const next = !enabled;
    setEnabled(next); // optimistic
    setStatus(null);
    startTransition(async () => {
      const res = await toggleConnectorAction(layer, next);
      if (res.ok) {
        setStatus({ tone: "ok", text: next ? "Enabled — banner will respect this layer." : "Disabled — layer no longer counts." });
      } else {
        setEnabled(!next); // rollback
        setStatus({ tone: "err", text: res.error ?? "Failed to update connector." });
      }
    });
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={onClick}
        disabled={pending}
        aria-pressed={enabled}
        className={`inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-xs font-medium transition ${
          enabled
            ? "border-accent/50 bg-accent/15 text-accent hover:bg-accent/25"
            : "border-edge bg-ink/40 text-muted hover:bg-ink/60"
        } ${pending ? "opacity-50" : ""}`}
      >
        <span
          aria-hidden
          className={`h-1.5 w-1.5 rounded-full ${enabled ? "bg-accent" : "bg-muted"}`}
        />
        {pending ? "Saving…" : enabled ? "Enabled" : "Disabled"}
      </button>
      {status && (
        <span className={`text-[11px] ${status.tone === "ok" ? "text-good" : "text-warn"}`}>
          {status.text}
        </span>
      )}
    </div>
  );
}
