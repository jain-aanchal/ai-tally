// SPDX-License-Identifier: Apache-2.0
// Layout kit for the dashboard visual refresh (CTO-221, D1): a dense KPI tile and the row wrapper
// pages lay them out in. This ticket only builds the primitives; no existing page is restyled here.
//
// Honesty is baked in, not optional: the value renders through <Money> and the delta through <Pct>,
// so an unknown number is the honest blank with a reason on hover, never a fabricated or zeroed
// figure. A tile whose `micro` can be null must pass a `reason`, exactly as the Money primitive
// requires, so the blank is always explained.

import type { ReactNode } from "react";

import { Money, Pct } from "@/components/HonestValue";
import type { MicroUSD } from "@/lib/types";

export interface SummaryTileProps {
  label: string;
  /** The headline value in micro-USD. null renders the honest blank (reason required then). */
  micro: MicroUSD | null;
  /** Required when `micro` can be null: the reason shown on the blank. */
  reason?: string;
  /** Small caption under the value (e.g. "last 30 days"). */
  hint?: string;
  /** Period-over-period change as a fraction (0.12 = +12%). null renders a blank delta. */
  delta?: number | null;
  /** Reason for a null delta. */
  deltaReason?: string;
  /**
   * Whether a rising value is good. Cost tiles leave this false, so an increase is tinted bad and a
   * decrease good; a value or ROI tile passes true to flip the coloring. A zero delta is neutral.
   */
  higherIsBetter?: boolean;
  /** Optional sparkline series in micro-USD, oldest to newest. */
  sparkline?: readonly number[];
}

export function SummaryTile({
  label,
  micro,
  reason,
  hint,
  delta,
  deltaReason,
  higherIsBetter = false,
  sparkline,
}: SummaryTileProps) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-edge bg-panel p-4">
      <span className="text-xs font-medium uppercase tracking-wide text-muted">{label}</span>
      <div className="flex items-end justify-between gap-2">
        <span className="text-2xl font-semibold tabular-nums">
          {/* Money's type requires a reason exactly when micro is nullable; forward whatever we got. */}
          <Money micro={micro} reason={reason ?? "no data for this period"} />
        </span>
        {sparkline && sparkline.length > 1 && <Sparkline values={sparkline} />}
      </div>
      <div className="flex items-center gap-2 text-xs">
        {delta !== undefined && (
          <DeltaBadge delta={delta} reason={deltaReason} higherIsBetter={higherIsBetter} />
        )}
        {hint && <span className="text-muted">{hint}</span>}
      </div>
    </div>
  );
}

function DeltaBadge({
  delta,
  reason,
  higherIsBetter,
}: {
  delta: number | null;
  reason?: string;
  higherIsBetter: boolean;
}) {
  if (delta === null || Number.isNaN(delta)) {
    // Blank delta with a reason: an unknown change is not "no change".
    return (
      <span className="text-muted">
        <Pct value={null} reason={reason ?? "no prior period to compare"} />
      </span>
    );
  }
  const rising = delta > 0;
  const flat = delta === 0;
  const good = flat ? null : rising === higherIsBetter;
  const tone = good === null ? "text-muted" : good ? "text-good" : "text-bad";
  const arrow = flat ? "→" : rising ? "▲" : "▼";
  return (
    <span className={`inline-flex items-center gap-1 tabular-nums ${tone}`}>
      <span aria-hidden>{arrow}</span>
      <Pct value={Math.abs(delta)} />
    </span>
  );
}

/** A tiny inline sparkline. No chart library; a single polyline scaled to its own min/max. */
function Sparkline({ values }: { values: readonly number[] }) {
  const w = 72;
  const h = 24;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`)
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      width={w}
      height={h}
      role="img"
      aria-label="trend sparkline"
      className="shrink-0"
    >
      <polyline points={points} fill="none" stroke="#5e81ac" strokeWidth="1.5" />
    </svg>
  );
}

/** Responsive grid the tiles sit in. Defaults to a 4-up row that reflows down on narrow screens. */
export function TileGrid({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">{children}</div>
  );
}
