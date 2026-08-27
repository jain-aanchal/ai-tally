// SPDX-License-Identifier: Apache-2.0
// The burn-down chart (CTO-210, F6). Hand-rolled inline SVG, no chart library, same house style as
// `StackedBarChart.tsx` and `Histogram.tsx`: deterministic, server-renderable, theme tokens only.
//
// WHAT IT DRAWS, in the order the eye should pick it up:
//
//   1. The cone. Everything after the last settled day, between the low and high edges of the
//      band. It is wide early and pinches shut on the closing day because that is genuinely how
//      much we know, and it is drawn FIRST so the lines sit on top of it rather than inside it.
//   2. Cumulative settled spend, solid. Measured, and it stops dead at the last settled day.
//   3. The point projection, dashed, continuing from where the actual stops.
//   4. The budget, a flat reference line.
//   5. The breach date, where the projection crosses the budget, labelled ON THE CHART. This is
//      the single most useful output of the whole feature, so it gets a marker, a rule and a date
//      in words rather than being left for the reader to eyeball off two intersecting lines.
//   6. The naive run-rate, faint, as the sanity line. A reader who does not trust the weekday
//      weighting can check it against the dumbest possible arithmetic without leaving the page.
//
// THE X AXIS IS BUILT FROM THE DATES IN `points`, WHICH CAME FROM CLICKHOUSE. There is no
// `new Date()` in this file and there must never be one. `queryCostSeries` once built its axis from
// the Node clock (CTO-203, since fixed) and the failure mode was silence: the oldest day slid off
// the chart while still counting toward the total printed beside it. Everything plotted here is
// indexed off the array it was handed.
//
// This component renders a cone unconditionally, so the CALLER is responsible for not rendering it
// at all below the minimum history. `BurndownCard` does that; `points` being empty is the belt to
// its braces and produces an explicit "nothing plotted" frame rather than an empty box.

import type { BreachForecast, BurndownPoint } from "@/lib/forecast";
import { formatUSD } from "@/lib/types";

const W = 720;
const H = 260;
// Wide enough for a formatted six-figure dollar tick ("$122,588.12") at 9px without clipping it
// against the left edge, which a narrower gutter silently does on any tenant spending six figures.
const PAD_L = 82;
const PAD_R = 16;
const PAD_T = 16;
const PAD_B = 34;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

/** Theme tokens, inlined because SVG `fill`/`stroke` cannot read Tailwind classes. */
const COLOR = {
  actual: "#5e81ac", // accent
  projection: "#26b5ce",
  cone: "#26b5ce",
  budget: "#cc9a1f", // warn
  breach: "#bf616a", // bad
  naive: "#4c566a", // muted
  axis: "#d8dee9", // edge
  text: "#4c566a",
};

export interface BurndownChartProps {
  /** The cone series from `forecastSpend`, oldest to newest. One point per calendar day. */
  points: readonly BurndownPoint[];
  /** Budget for the period, or null when none is set: then no reference line is drawn. */
  budgetMicroUsd: number | null;
  /** The engine's breach verdict. Only `breaches` puts a marker on the chart. */
  breach: BreachForecast;
  /** `spend_so_far / days_elapsed * days_in_period`, the sanity line. Null hides it. */
  naiveRunRateMicroUsd: number | null;
  /** Last settled day: where the solid line stops and the cone starts. */
  asOf: string | null;
}

export function BurndownChart({
  points,
  budgetMicroUsd,
  breach,
  naiveRunRateMicroUsd,
  asOf,
}: BurndownChartProps) {
  if (points.length === 0) {
    return (
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="no burn-down to plot" className="w-full">
        <text x={W / 2} y={H / 2} fontSize="12" fill={COLOR.text} textAnchor="middle">
          nothing to plot for this period yet
        </text>
      </svg>
    );
  }

  const n = points.length;
  // One column per calendar day, first day at the left edge and last day at the right edge, so the
  // period boundaries are the plot boundaries and "the 22nd" lands where a reader expects it.
  const x = (i: number) => PAD_L + (n === 1 ? PLOT_W / 2 : (i / (n - 1)) * PLOT_W);

  const candidates = [
    ...points.map((p) => p.highMicroUsd),
    ...points.map((p) => p.projectedMicroUsd),
    ...points.map((p) => p.actualMicroUsd ?? 0),
  ];
  if (budgetMicroUsd !== null) candidates.push(budgetMicroUsd);
  if (naiveRunRateMicroUsd !== null) candidates.push(naiveRunRateMicroUsd);
  // 1.06 headroom keeps the budget line and its label off the top edge. Max(1, …) guards the
  // all-zero case, which would otherwise divide by zero and emit NaN into every coordinate.
  const yMax = Math.max(1, ...candidates) * 1.06;
  const y = (v: number) => PAD_T + PLOT_H - (v / yMax) * PLOT_H;

  // The measured span. `asOf` is a date rather than an index because it arrives from ClickHouse;
  // this is the one place the two are related, and it is a lookup, never arithmetic on a clock.
  const asOfIdx = asOf === null ? -1 : points.findIndex((p) => p.date === asOf);
  const lastActualIdx = points.reduce(
    (acc, p, i) => (p.actualMicroUsd !== null ? i : acc),
    -1,
  );
  const boundaryIdx = asOfIdx >= 0 ? asOfIdx : lastActualIdx;

  const actual = points
    .map((p, i) => (p.actualMicroUsd === null ? null : `${x(i)},${y(p.actualMicroUsd)}`))
    .filter((s): s is string => s !== null)
    .join(" ");

  // The cone and the dashed projection both start at the boundary so they visibly continue the
  // solid line rather than floating away from it.
  const coneFrom = Math.max(boundaryIdx, 0);
  const coneIdx = points.map((_, i) => i).filter((i) => i >= coneFrom);
  const conePath =
    coneIdx.length > 1
      ? [
          ...coneIdx.map((i) => `${x(i)},${y(points[i].highMicroUsd)}`),
          ...[...coneIdx].reverse().map((i) => `${x(i)},${y(points[i].lowMicroUsd)}`),
        ].join(" ")
      : null;
  const projection =
    coneIdx.length > 1
      ? coneIdx.map((i) => `${x(i)},${y(points[i].projectedMicroUsd)}`).join(" ")
      : null;

  const budgetY = budgetMicroUsd === null ? null : y(budgetMicroUsd);

  // Only a real crossing gets a marker. `never`, `no_budget` and `cannot_project` each mean
  // something different and none of them is "a line to draw here"; the card states them in words.
  const breachIdx =
    breach.outcome === "breaches" && breach.date !== null
      ? points.findIndex((p) => p.date === breach.date)
      : -1;

  const naiveFrom = boundaryIdx >= 0 ? boundaryIdx : 0;
  const naiveLine =
    naiveRunRateMicroUsd !== null && naiveFrom < n - 1
      ? {
          x1: x(naiveFrom),
          y1: y(points[naiveFrom].projectedMicroUsd),
          x2: x(n - 1),
          y2: y(naiveRunRateMicroUsd),
        }
      : null;

  // Roughly six gridlines, on round dollar steps of the y range.
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const labelEvery = Math.max(1, Math.ceil(n / 8));

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={chartLabel(points, breach, budgetMicroUsd)}
      className="w-full"
    >
      {ticks.map((t) => (
        <g key={t}>
          <line x1={PAD_L} x2={W - PAD_R} y1={y(t)} y2={y(t)} stroke={COLOR.axis} />
          <text x={PAD_L - 6} y={y(t) + 3} fontSize="9" fill={COLOR.text} textAnchor="end">
            {formatUSD(t)}
          </text>
        </g>
      ))}

      {conePath && (
        <polygon points={conePath} fill={COLOR.cone} opacity="0.16">
          <title>
            {`80% band: it is widest just after the last settled day and closes to nothing on ${points[n - 1].date}`}
          </title>
        </polygon>
      )}

      {/* The settled/projected boundary, same idiom as the reconciled/estimated line on the
          stacked chart, so the two charts on this page read the same way. */}
      {boundaryIdx >= 0 && boundaryIdx < n - 1 && (
        <g>
          <line
            x1={x(boundaryIdx)}
            x2={x(boundaryIdx)}
            y1={PAD_T}
            y2={H - PAD_B}
            stroke={COLOR.text}
            strokeDasharray="4 3"
          />
          <text x={x(boundaryIdx) + 4} y={PAD_T + 10} fontSize="9" fill={COLOR.text}>
            ← settled · projected →
          </text>
        </g>
      )}

      {naiveLine && (
        <line
          x1={naiveLine.x1}
          y1={naiveLine.y1}
          x2={naiveLine.x2}
          y2={naiveLine.y2}
          stroke={COLOR.naive}
          strokeWidth="1.5"
          strokeDasharray="1 4"
        >
          <title>
            {`Naive run-rate, the sanity line: ${formatUSD(naiveRunRateMicroUsd ?? 0)} by ${points[n - 1].date}. Spend to date divided by days elapsed, with no weekday weighting.`}
          </title>
        </line>
      )}

      {projection && (
        <polyline
          points={projection}
          fill="none"
          stroke={COLOR.projection}
          strokeWidth="2"
          strokeDasharray="5 4"
        >
          <title>
            {`Day-of-week weighted projection: ${formatUSD(points[n - 1].projectedMicroUsd)} by ${points[n - 1].date}`}
          </title>
        </polyline>
      )}

      {actual && (
        <polyline points={actual} fill="none" stroke={COLOR.actual} strokeWidth="2.5">
          <title>
            {lastActualIdx >= 0
              ? `Cumulative settled spend: ${formatUSD(points[lastActualIdx].actualMicroUsd ?? 0)} through ${points[lastActualIdx].date}`
              : "Cumulative settled spend"}
          </title>
        </polyline>
      )}

      {budgetY !== null && (
        <g>
          <line
            x1={PAD_L}
            x2={W - PAD_R}
            y1={budgetY}
            y2={budgetY}
            stroke={COLOR.budget}
            strokeWidth="1.5"
            strokeDasharray="6 4"
          />
          <text x={PAD_L + 4} y={budgetY - 5} fontSize="10" fill={COLOR.budget}>
            budget {formatUSD(budgetMicroUsd ?? 0)}
          </text>
        </g>
      )}

      {/* THE BREACH DATE. The one number a reader should be able to take away without reading a
          word of the card, so it is a rule, a dot and a date, not an intersection to squint at. */}
      {breachIdx >= 0 && budgetY !== null && (
        <g>
          <line
            x1={x(breachIdx)}
            x2={x(breachIdx)}
            y1={PAD_T}
            y2={H - PAD_B}
            stroke={COLOR.breach}
            strokeWidth="1.5"
          />
          <circle cx={x(breachIdx)} cy={budgetY} r="4" fill={COLOR.breach} />
          <text
            x={breachIdx > n * 0.6 ? x(breachIdx) - 6 : x(breachIdx) + 6}
            // Below the settled/projected boundary caption rather than beside it: on a late-month
            // breach the two labels sit at the same x and would otherwise overprint each other.
            y={PAD_T + 44}
            fontSize="11"
            fontWeight="600"
            fill={COLOR.breach}
            textAnchor={breachIdx > n * 0.6 ? "end" : "start"}
          >
            crosses budget {points[breachIdx].date}
          </text>
        </g>
      )}

      {points.map((p, i) =>
        // The last day always gets a label (it is the period end); any regular tick within one
        // label-interval of it is dropped so the two do not overprint. On a 31-day month with
        // labelEvery=4 the regular tick at day 29 (i=28) otherwise collides with the day-31 label at
        // the right edge; requiring a full interval of clearance (i <= n-1-labelEvery) fixes it.
        (i % labelEvery === 0 && i <= n - 1 - labelEvery) || i === n - 1 ? (
          <text
            key={p.date}
            x={x(i)}
            y={H - 12}
            fontSize="9"
            fill={COLOR.text}
            textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
          >
            {p.date.slice(5)}
          </text>
        ) : null,
      )}
    </svg>
  );
}

/**
 * The chart's own sentence, for a reader who cannot see it. It says the breach outcome rather than
 * describing the shapes, because the outcome is the content and "a line chart with a shaded region"
 * is not.
 */
function chartLabel(
  points: readonly BurndownPoint[],
  breach: BreachForecast,
  budgetMicroUsd: number | null,
): string {
  const end = points[points.length - 1];
  const head = `Cumulative spend burn-down through ${end.date}, projected to ${formatUSD(end.projectedMicroUsd)}`;
  switch (breach.outcome) {
    case "breaches":
      return `${head}, crossing the ${formatUSD(budgetMicroUsd ?? 0)} budget on ${breach.date}.`;
    case "never":
      return `${head}, staying under the ${formatUSD(budgetMicroUsd ?? 0)} budget for the whole period.`;
    case "no_budget":
      return `${head}. No budget is set, so no reference line is drawn.`;
    default:
      return `${head}.`;
  }
}

/** The key. Separate from the SVG so it wraps as text and stays readable at any width. */
export function BurndownLegend({ hasBudget }: { hasBudget: boolean }) {
  const items: { color: string; label: string; dash?: string }[] = [
    { color: COLOR.actual, label: "Settled spend to date" },
    { color: COLOR.projection, label: "Weighted projection", dash: "dashed" },
    { color: COLOR.cone, label: "80% confidence band" },
    { color: COLOR.naive, label: "Naive run-rate (sanity line)", dash: "dotted" },
  ];
  if (hasBudget) items.push({ color: COLOR.budget, label: "Budget", dash: "dashed" });
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: it.color }}
          />
          {it.label}
        </li>
      ))}
    </ul>
  );
}
