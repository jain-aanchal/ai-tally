// SPDX-License-Identifier: Apache-2.0
// Daily direct cost for one account (CTO-190, plan D4).
//
// A single-series bar chart rather than the stacked one from /cost. StackedBarChart takes a
// CostSeries carrying all six layers plus a reconciled/estimated boundary, and neither applies
// here: this account's spend is direct-only by construction (compute and egress carry no account),
// and the rollup this reads has no reconciliation boundary in it. Feeding it a fabricated six-layer
// series with two permanent zeroes and an invented boundary would draw a chart that says more than
// we know. Same hand-rolled SVG house style, no chart library, deterministic output.

import { type AccountTrendPoint, trendTotal } from "@/lib/accounts";
import { LAYER_COLORS } from "@/lib/cost";
import { formatUSD } from "@/lib/types";

export function TrendChart({ trend }: { trend: readonly AccountTrendPoint[] }) {
  const W = 720;
  const H = 180;
  const PAD_L = 8;
  const PAD_R = 8;
  const PAD_T = 12;
  const PAD_B = 24;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;
  const n = trend.length;

  if (n === 0) {
    return (
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="no trend data" className="w-full">
        <text x={W / 2} y={H / 2} fontSize="12" fill="#8a93a6" textAnchor="middle">
          no daily cost recorded for this account
        </text>
      </svg>
    );
  }

  const step = plotW / n;
  const barW = step * 0.7;
  // A quiet account can be all zeroes across the window, which is a true and useful thing to see.
  // The floor of 1 only keeps the height arithmetic from dividing by zero and emitting NaN into
  // every <rect>; it never inflates a real bar, because every bar in that case is zero anyway.
  const max = Math.max(1, ...trend.map((p) => p.directCostMicroUsd));
  const total = trendTotal(trend);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`daily direct cost for this account, ${n} days totalling ${formatUSD(total)}`}
      className="w-full"
    >
      {trend.map((p, i) => {
        const cx = PAD_L + i * step + step / 2;
        const h = (p.directCostMicroUsd / max) * plotH;
        return (
          <g key={p.date}>
            <rect
              x={cx - barW / 2}
              y={H - PAD_B - h}
              width={barW}
              // A day with real but tiny spend must not render as nothing: a sub-pixel bar is
              // visually identical to a zero day, and those are different facts. Anything above
              // zero gets at least one pixel.
              height={p.directCostMicroUsd > 0 ? Math.max(1, h) : 0}
              fill={LAYER_COLORS.llm}
            >
              <title>{`${p.date} · ${formatUSD(p.directCostMicroUsd)}`}</title>
            </rect>
            {i % 5 === 0 ? (
              <text x={cx} y={H - 8} fontSize="9" fill="#8a93a6" textAnchor="middle">
                {p.date.slice(5)}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}
