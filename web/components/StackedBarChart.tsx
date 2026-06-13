// SPDX-License-Identifier: Apache-2.0
// Hand-rolled SVG stacked-bar chart by layer over time, with a vertical boundary
// separating reconciled (past) from estimated (recent). No chart library; deterministic.

import { LAYER_COLORS, LAYER_LABEL, LAYERS, type Layer, totalForDay, type CostSeries } from "@/lib/cost";

export function StackedBarChart({ series }: { series: CostSeries }) {
  const W = 720;
  const H = 220;
  const PAD_L = 40;
  const PAD_R = 16;
  const PAD_T = 12;
  const PAD_B = 28;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;
  const n = series.days.length;
  const barW = (plotW / n) * 0.7;
  const step = plotW / n;

  const maxTotal = Math.max(...series.days.map(totalForDay));

  // boundary x: just after the last reconciled day
  const reconciledIdx = series.days.findIndex((d) => d.date > series.reconciledThrough);
  const boundaryX = PAD_L + (reconciledIdx === -1 ? plotW : reconciledIdx * step);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="stacked cost by layer over time" className="w-full">
      {/* boundary band: estimated region */}
      <rect
        x={boundaryX}
        y={PAD_T}
        width={W - PAD_R - boundaryX}
        height={plotH}
        fill="#252b37"
        opacity="0.35"
      />
      <line x1={boundaryX} x2={boundaryX} y1={PAD_T} y2={H - PAD_B} stroke="#8a93a6" strokeDasharray="4 3" />
      <text x={boundaryX + 4} y={PAD_T + 12} fontSize="10" fill="#8a93a6">
        ← reconciled · estimated →
      </text>

      {series.days.map((d, i) => {
        const cx = PAD_L + i * step + step / 2;
        let yCursor = H - PAD_B;
        return (
          <g key={d.date}>
            {LAYERS.map((l) => {
              const v = d.byLayer[l];
              const h = (v / maxTotal) * plotH;
              yCursor -= h;
              return (
                <rect
                  key={l}
                  x={cx - barW / 2}
                  y={yCursor}
                  width={barW}
                  height={h}
                  fill={LAYER_COLORS[l]}
                >
                  <title>{`${d.date} · ${LAYER_LABEL[l]}: $${(v / 1_000_000).toFixed(2)}`}</title>
                </rect>
              );
            })}
            {i % 3 === 0 && (
              <text x={cx} y={H - 10} fontSize="9" fill="#8a93a6" textAnchor="middle">
                {d.date.slice(5)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export function Legend() {
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {LAYERS.map((l) => (
        <li key={l} className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: LAYER_COLORS[l as Layer] }}
          />
          {LAYER_LABEL[l]}
        </li>
      ))}
    </ul>
  );
}
