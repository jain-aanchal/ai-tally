// SPDX-License-Identifier: Apache-2.0
// A tiny inline sparkline (CTO-221 D1, extracted to its own module in CTO-244 so the SummaryTile and
// the Cost explorer's breakdown table draw the same shape instead of two copies drifting apart).
//
// No chart library: a single polyline scaled to its own min/max. Theme-token coloured via
// `currentColor` so a caller sets the stroke through a text-* class (default frost accent) and it
// tracks the palette rather than a hard-coded hex.

export interface SparklineProps {
  /** The series to draw, oldest to newest. */
  values: readonly number[];
  width?: number;
  height?: number;
  /** Overrides the default text-accent stroke colour; any text-* token class works. */
  className?: string;
  ariaLabel?: string;
}

export function Sparkline({
  values,
  width = 72,
  height = 24,
  className = "text-accent",
  ariaLabel = "trend sparkline",
}: SparklineProps) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`)
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      aria-label={ariaLabel}
      className={`shrink-0 ${className}`.trim()}
    >
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
