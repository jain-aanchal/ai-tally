// SPDX-License-Identifier: Apache-2.0
// Honest-value primitives (CTO-178, plan A2). "Honest under uncertainty" is the product's central
// posture: where we do not know a number we render a blank instead of fabricating one. That blank
// was hand-written across 10 files, and the reason it is blank was either missing or an ad-hoc
// `title`. One component, so a reader can always find out WHY a cell is empty.

import { formatUSD, type MicroUSD } from "@/lib/types";

/** The blank glyph itself. Exported so call sites can assert on it without re-typing it. */
export const BLANK = "—";

/**
 * A value we do not have, with the reason attached.
 *
 * The reason is carried three ways because each reaches a different reader: `title` for the mouse,
 * an sr-only sentence for assistive tech, and a dotted underline plus help cursor so a sighted
 * reader knows there is something to hover at all. A bare glyph with only a `title` is invisible
 * as an affordance, which is how blanks ended up feeling like bugs.
 */
export function Blank({ reason, className = "" }: { reason: string; className?: string }) {
  return (
    <span
      title={reason}
      className={`cursor-help text-muted underline decoration-dotted decoration-muted/60 underline-offset-4 ${className}`.trim()}
    >
      <span aria-hidden>{BLANK}</span>
      <span className="sr-only">No value: {reason}</span>
    </span>
  );
}

/**
 * `reason` is required exactly when the value can be null. A caller passing a definite number is
 * not asked to invent an explanation, and a caller passing a nullable one cannot forget to give
 * one. The whole point of the primitive is that the blank is never unexplained.
 */
type NullableProps<K extends string, V> =
  | ({ [P in K]: V } & { reason?: string; className?: string })
  | ({ [P in K]: V | null } & { reason: string; className?: string });

/** Money, in the micro-USD integers the wire and mock layers both speak. */
export function Money({
  micro,
  reason,
  className,
}: NullableProps<"micro", MicroUSD>) {
  if (micro === null || Number.isNaN(micro)) {
    return <Blank reason={reason ?? "no cost data for this period"} className={className} />;
  }
  return <span className={className}>{formatUSD(micro)}</span>;
}

/**
 * A percentage. `value` is a fraction (0.42 renders "42.0%"), matching how rates arrive from the
 * API everywhere in this app. `digits` because the existing surfaces disagree on precision:
 * attribution rates round to whole percent, error rates want two decimals.
 *
 * `unit` exists for ranges. A confidence band reads "9.9–10.3%", printing the sign once at the end
 * rather than on both ends, so the low bound needs the same rounding as everything else but not the
 * trailing "%". Without this the only options are re-implementing the formatting at the call site
 * or changing how the band reads, and the band's format is deliberate.
 */
export function Pct({
  value,
  digits = 1,
  unit = true,
  reason,
  className,
}: NullableProps<"value", number> & { digits?: number; unit?: boolean }) {
  if (value === null || Number.isNaN(value)) {
    return <Blank reason={reason ?? "not enough samples to report a rate"} className={className} />;
  }
  return (
    <span className={className}>
      {(value * 100).toFixed(digits)}
      {unit ? "%" : ""}
    </span>
  );
}
