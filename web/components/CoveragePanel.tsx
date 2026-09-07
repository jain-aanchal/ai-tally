// SPDX-License-Identifier: Apache-2.0
// Per-layer coverage panel (CTO-261, onboarding-agent §4.1 / §7).
//
// What the developer needs to see after a wired change runs is not "connected", it is WHICH of the
// five layers is actually flowing and, for every layer that is not, why. So each row states its
// case: a covered layer shows the span count that proves it, a dark layer says whether it is
// awaiting its first event or simply unwired, and a layer we could not read renders the honest
// blank with the reason on hover rather than a zero (CLAUDE.md).
//
// The proving-span count goes through `Blank` for exactly that reason: an unknown count is a blank
// with a reason, never the number 0, which would read as "this layer fired nothing" when the truth
// is "we could not look".
"use client";

import { useEffect, useState } from "react";

import { Blank } from "@/components/HonestValue";
import {
  COVERAGE_LAYER_LABELS,
  type LayerCoverage,
  type LayerCoverageState,
  unknownCoverage,
} from "@/lib/firstEvent";

const POLL_MS = 5000;

/** One row's badge. Wording is the developer-facing half of §7's honesty rule. */
const STATE_COPY: Record<LayerCoverageState, { label: string; className: string }> = {
  covered: { label: "Flowing", className: "border-good/40 bg-good/10 text-good" },
  awaiting_first_event: {
    label: "Wired, awaiting first event",
    className: "border-warn/40 bg-warn/10 text-warn",
  },
  not_wired: { label: "Not wired", className: "border-edge bg-ink text-muted" },
  unknown: { label: "Unknown", className: "border-edge bg-ink text-muted" },
};

export function CoveragePanel({
  initialLayers,
  wired = [],
  poll = true,
}: {
  /** Server-rendered first answer, so the panel never flashes an empty state it has to correct. */
  initialLayers?: LayerCoverage[];
  /** Layers the onboarding agent reports it wired. Only ever softens a dark layer's wording. */
  wired?: readonly string[];
  poll?: boolean;
}) {
  const [layers, setLayers] = useState<LayerCoverage[]>(
    initialLayers ??
      unknownCoverage("the coverage probe has not answered yet, so we cannot tell yet"),
  );

  useEffect(() => {
    if (!poll) return;
    const qs = wired.length ? `?wired=${encodeURIComponent(wired.join(","))}` : "";
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(`/api/onboarding/coverage${qs}`, { cache: "no-store" });
        const data = (await res.json()) as { layers?: LayerCoverage[] };
        // Keep the last honest answer on a failed poll rather than blanking a proven layer: a
        // dropped request is not evidence that instrumentation stopped.
        if (!cancelled && Array.isArray(data.layers) && data.layers.length) {
          setLayers(data.layers);
        }
      } catch {
        /* keep the last answer and try again */
      }
    };
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // `wired` is a prop array; join it so a re-render with an equal list does not restart the poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poll, wired.join(",")]);

  const covered = layers.filter((l) => l.state === "covered").length;
  const unknown = layers.filter((l) => l.state === "unknown").length;

  return (
    <section className="rounded-xl border border-edge bg-panel p-5">
      <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-muted">
        Instrumentation coverage
      </h2>
      <p className="mb-4 text-sm text-muted">
        A layer counts as flowing only when a real span proves it. Layers we have no span for are
        named with the reason, and a layer we could not read is left blank rather than called dark.
      </p>

      <ul className="space-y-2">
        {layers.map((layer) => {
          const copy = STATE_COPY[layer.state];
          return (
            <li
              key={layer.layer}
              className="flex items-center justify-between gap-4 rounded-lg border border-edge bg-ink px-4 py-3"
            >
              <div className="min-w-0">
                <div className="text-sm text-fg">{COVERAGE_LAYER_LABELS[layer.layer]}</div>
                <div className="mt-0.5 text-xs text-muted">{layer.reason}</div>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <span className="text-xs text-muted" title="spans proving this layer">
                  {layer.provingSpans === null ? (
                    <Blank reason={layer.reason} />
                  ) : (
                    `${layer.provingSpans.toLocaleString()} spans`
                  )}
                </span>
                <span
                  className={`rounded-md border px-2 py-1 text-xs ${copy.className}`}
                >
                  {copy.label}
                </span>
              </div>
            </li>
          );
        })}
      </ul>

      <div className="mt-4 border-t border-edge pt-3 text-xs text-muted">
        {covered}/{layers.length} layers proven by a span
        {unknown > 0 ? `, ${unknown} could not be read` : ""}
      </div>
    </section>
  );
}
