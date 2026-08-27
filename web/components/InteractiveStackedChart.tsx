// SPDX-License-Identifier: Apache-2.0
// The interactive stacked-bar chart the dashboard overhaul is built on (CTO-221, D1).
//
// WHY A NEW COMPONENT rather than upgrading StackedBarChart in place: the existing chart is typed to
// CostSeries and the six fixed cost LAYERS, and its whole shape (LAYER_COLORS, the reconciled/
// estimated boundary band) assumes them. The explorer groups by an ARBITRARY dimension chosen at
// runtime (feature / model / provider / account), so it needs a chart keyed on a generic group list,
// not on Layer. Mutating the shipped /cost chart to be generic would also have restyled an existing
// page, which this ticket forbids. So this is the interactive variant the ticket explicitly allows,
// and the reason is recorded here and in the PR body.
//
// It stays in the house style: hand-rolled inline SVG, no charting library (the CSP / artifact
// posture forbids external chart deps), deterministic, one bar per calendar day. What it adds over
// StackedBarChart:
//   - a hover tooltip naming the group, date and value under the cursor;
//   - legend toggling (click a legend item to hide/show that series; the y-axis rescales to what is
//     visible so a hidden dominant series lets the rest breathe);
//   - an onDrill(group) callback so a page can turn a click on a bar or legend into a filter.
//
// Theme: axis, gridlines, crosshair, tooltip surface and the empty band read from the --chart-*
// CSS variables (see globals.css), so the chart is correct in dark today and tracks a light palette
// if one is applied. Series colors are data identity and are passed in by the caller.

"use client";

import { useMemo, useState } from "react";

import { formatUSD, type MicroUSD } from "@/lib/types";

export interface StackedChartDay {
  /** ISO yyyy-mm-dd. Supplied by the caller from a ClickHouse-derived day list, never a JS clock. */
  date: string;
  /** Micro-USD per group for this day. A missing group is a real zero. */
  byGroup: Record<string, MicroUSD>;
}

export interface InteractiveStackedChartProps {
  days: readonly StackedChartDay[];
  /** Stacking order, bottom to top, and legend order. */
  groups: readonly string[];
  /** Color per group. Falls back to a built-in categorical palette by index. */
  color?: (group: string, index: number) => string;
  /** Human label per group. Defaults to the group key. */
  label?: (group: string) => string;
  /** Clicking a bar segment or legend swatch calls this (the page turns it into a filter). */
  onDrill?: (group: string) => void;
  /** Render the built-in interactive legend below the chart. Default true. */
  showLegend?: boolean;
  ariaLabel?: string;
  emptyLabel?: string;
  /** Formats the tooltip value. Defaults to formatUSD (micro-USD in). */
  formatValue?: (micro: MicroUSD) => string;
  height?: number;
}

// A brand-adjacent categorical palette for arbitrary groups. Ordered so the top few (the usual big
// spenders) are the most distinct; it repeats past its length, which the group cap (MAX_EXPLORE_
// GROUPS) keeps us well short of in practice.
const PALETTE = [
  "#5e81ac", // accent
  "#26b5ce",
  "#4c9f70", // good
  "#cc9a1f", // warn
  "#bb87fc",
  "#bf616a", // bad
  "#5cc8ff",
  "#f2994a",
  "#7ed3b2",
  "#c58af9",
  "#9aa4b2",
  "#e57ea8",
];

function defaultColor(_group: string, index: number): string {
  return PALETTE[index % PALETTE.length];
}

const W = 720;
const PAD_L = 44;
const PAD_R = 16;
const PAD_T = 12;
const PAD_B = 28;

export function InteractiveStackedChart({
  days,
  groups,
  color = defaultColor,
  label = (g) => g,
  onDrill,
  showLegend = true,
  ariaLabel = "stacked cost over time",
  emptyLabel = "no data for this filter yet",
  formatValue = formatUSD,
  height = 220,
}: InteractiveStackedChartProps) {
  const H = height;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  // Hidden series live in component state, not the URL: this is a transient view tweak, not a
  // shareable filter (that is what onDrill + the FilterBar are for).
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [hover, setHover] = useState<{ day: number; group: string } | null>(null);

  const colorOf = useMemo(() => {
    const m = new Map<string, string>();
    groups.forEach((g, i) => m.set(g, color(g, i)));
    return (g: string) => m.get(g) ?? "#4c566a";
  }, [groups, color]);

  const visibleGroups = groups.filter((g) => !hidden.has(g));
  const n = days.length;

  const dayTotal = (d: StackedChartDay) => visibleGroups.reduce((s, g) => s + (d.byGroup[g] ?? 0), 0);
  // Max over VISIBLE groups so hiding the dominant series rescales the rest. Max(1, …) guards the
  // all-zero / all-hidden case, which would otherwise divide by zero and emit NaN per <rect>.
  const maxTotal = Math.max(1, ...days.map(dayTotal));

  if (n === 0) {
    return (
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={emptyLabel} className="w-full">
        <text
          x={W / 2}
          y={H / 2}
          fontSize="12"
          fill="var(--chart-muted)"
          textAnchor="middle"
        >
          {emptyLabel}
        </text>
      </svg>
    );
  }

  const step = plotW / n;
  const barW = step * 0.7;

  const hoverDay = hover ? days[hover.day] : null;
  const hoverValue = hover && hoverDay ? hoverDay.byGroup[hover.group] ?? 0 : 0;

  return (
    <div className="w-full">
      <div className="relative">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel} className="w-full">
          {/* baseline */}
          <line
            x1={PAD_L}
            x2={W - PAD_R}
            y1={H - PAD_B}
            y2={H - PAD_B}
            stroke="var(--chart-axis)"
          />
          {days.map((d, i) => {
            const cx = PAD_L + i * step + step / 2;
            let yCursor = H - PAD_B;
            return (
              <g key={d.date}>
                {visibleGroups.map((g) => {
                  const v = d.byGroup[g] ?? 0;
                  const h = (v / maxTotal) * plotH;
                  yCursor -= h;
                  const isHovered = hover?.day === i && hover?.group === g;
                  return (
                    <rect
                      key={g}
                      x={cx - barW / 2}
                      y={yCursor}
                      width={barW}
                      height={h}
                      fill={colorOf(g)}
                      opacity={hover && !isHovered ? 0.55 : 1}
                      cursor={onDrill ? "pointer" : "default"}
                      onMouseEnter={() => setHover({ day: i, group: g })}
                      onMouseLeave={() => setHover(null)}
                      onClick={() => onDrill?.(g)}
                    />
                  );
                })}
                {i % Math.max(1, Math.ceil(n / 10)) === 0 && (
                  <text
                    x={cx}
                    y={H - 10}
                    fontSize="9"
                    fill="var(--chart-muted)"
                    textAnchor="middle"
                  >
                    {d.date.slice(5)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {hover && hoverDay && (
          <Tooltip
            x={PAD_L + hover.day * step + step / 2}
            plotW={W}
            group={label(hover.group)}
            swatch={colorOf(hover.group)}
            date={hoverDay.date}
            value={formatValue(hoverValue)}
          />
        )}
      </div>

      {showLegend && (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
          {groups.map((g) => {
            const off = hidden.has(g);
            return (
              <li key={g}>
                <button
                  type="button"
                  onClick={() =>
                    setHidden((prev) => {
                      const next = new Set(prev);
                      if (next.has(g)) next.delete(g);
                      else next.add(g);
                      return next;
                    })
                  }
                  aria-pressed={!off}
                  className={`flex items-center gap-1.5 hover:text-fg ${off ? "opacity-40" : ""}`}
                  title={off ? `Show ${label(g)}` : `Hide ${label(g)}`}
                >
                  <span
                    aria-hidden
                    className="inline-block h-2.5 w-2.5 rounded-sm"
                    style={{
                      background: colorOf(g),
                      // A struck-through swatch reads as "off" without relying on color alone.
                      filter: off ? "grayscale(1)" : undefined,
                    }}
                  />
                  <span className={off ? "line-through" : ""}>{label(g)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * The hover tooltip. Positioned as a percentage of the chart width so it tracks the bar under the
 * responsive SVG (which scales to its container) rather than a fixed pixel that would drift. Flips to
 * the left of the cursor past the midpoint so it never runs off the right edge.
 */
function Tooltip({
  x,
  plotW,
  group,
  swatch,
  date,
  value,
}: {
  x: number;
  plotW: number;
  group: string;
  swatch: string;
  date: string;
  value: string;
}) {
  const pct = (x / plotW) * 100;
  const flip = pct > 60;
  return (
    <div
      className="pointer-events-none absolute top-1 z-10 whitespace-nowrap rounded-md border px-2 py-1 text-xs shadow-lg"
      style={{
        left: `${pct}%`,
        transform: flip ? "translateX(-100%) translateX(-8px)" : "translateX(8px)",
        background: "var(--chart-surface)",
        borderColor: "var(--chart-surface-border)",
        color: "var(--chart-muted)",
      }}
    >
      <div className="flex items-center gap-1.5">
        <span
          aria-hidden
          className="inline-block h-2 w-2 rounded-sm"
          style={{ background: swatch }}
        />
        <span className="font-medium text-fg">{group}</span>
      </div>
      <div className="mt-0.5 tabular-nums">
        {date} · <span className="text-fg">{value}</span>
      </div>
    </div>
  );
}
