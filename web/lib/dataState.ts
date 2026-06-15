// SPDX-License-Identifier: Apache-2.0
// Shared state-derivation for the "never show stale as fresh" requirement (CTO-80, spec 13.8).
//
// Every workflow surface derives one of four DataStates from the data it already receives, and
// renders the matching banner/badge so users can never mistake synthetic, partial, or stale
// numbers for fresh real data. These are pure functions — no I/O, no React — so they are unit
// testable and reused identically across all five workflows.

/** Two hours in milliseconds — the freshness threshold from spec 13.8. */
export const STALE_AFTER_MS = 2 * 60 * 60 * 1000;

/**
 * Far-past sentinel boundary. The seeded data uses `reconciledThrough: "1970-01-01"` to mean
 * "nothing has reconciled yet". Anything on or before this is treated as the empty/pre-data case,
 * NOT a stale warning. We use the start of 2000 as a generous cutoff: no real reconciliation
 * boundary will ever predate it, and epoch-style sentinels fall below it.
 */
export const SENTINEL_BEFORE_MS = Date.UTC(2000, 0, 1);

export type DataState = "empty" | "partial" | "stale" | "fresh";

/** How long ago, in ms, the reconciliation boundary is relative to `now`. */
export function ageMs(reconciledThrough: string, now: number = Date.now()): number {
  return now - Date.parse(reconciledThrough);
}

/**
 * Build a reconciliation-boundary ISO timestamp from "minutes since the reconciler last ran".
 * Surfaces that don't carry an explicit boundary date (Agents, Compare, Estimate) report freshness
 * as a minutes-ago number instead; this converts it so they share the same stale/fresh logic.
 */
export function boundaryFromMinutesAgo(minutesAgo: number, now: number = Date.now()): string {
  return new Date(now - minutesAgo * 60_000).toISOString();
}

/** A far-past sentinel boundary means nothing has reconciled yet (pre-data), not stale. */
export function isSentinelBoundary(reconciledThrough: string): boolean {
  const t = Date.parse(reconciledThrough);
  return Number.isNaN(t) || t <= SENTINEL_BEFORE_MS;
}

/** Stale = a real (non-sentinel) boundary older than the 2h freshness window. */
export function isStale(reconciledThrough: string, now: number = Date.now()): boolean {
  if (isSentinelBoundary(reconciledThrough)) return false;
  return ageMs(reconciledThrough, now) > STALE_AFTER_MS;
}

export interface DataStateInput {
  /** True when no real telemetry exists — render the synthetic preview + connector CTA. */
  isEmpty: boolean;
  /** True when some sources/layers are populated but others are missing. */
  isPartial: boolean;
  /** Reconciliation boundary timestamp, if the surface has one. */
  reconciledThrough?: string;
  /** Override "now" for testing. */
  now?: number;
}

/**
 * Resolve the single DataState for a surface. Precedence:
 *   empty  > stale > partial > fresh
 * Empty wins because there's nothing real to be stale or partial about. Stale outranks partial
 * because presenting stale numbers as fresh is the cardinal sin this ticket guards against.
 */
export function deriveDataState(input: DataStateInput): DataState {
  if (input.isEmpty) return "empty";
  if (input.reconciledThrough && isStale(input.reconciledThrough, input.now)) return "stale";
  if (input.isPartial) return "partial";
  return "fresh";
}

/** Human "data as of" label. Returns null for sentinel/unparseable boundaries. */
export function asOfLabel(reconciledThrough: string): string | null {
  const t = Date.parse(reconciledThrough);
  if (Number.isNaN(t) || t <= SENTINEL_BEFORE_MS) return null;
  return reconciledThrough;
}

/** Compact relative-age string, e.g. "3h ago", "2d ago", "just now". */
export function relativeAge(reconciledThrough: string, now: number = Date.now()): string {
  const ms = ageMs(reconciledThrough, now);
  if (ms < 60_000) return "just now";
  const mins = Math.floor(ms / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** True if every numeric value in a record is zero (or the record is empty). */
export function allZero(values: Record<string, number>): boolean {
  const xs = Object.values(values) as number[];
  return xs.length === 0 || xs.every((v) => v === 0);
}

/** True if some values are zero and some are non-zero — the signature of partial coverage. */
export function someZero(values: Record<string, number>): boolean {
  const xs = Object.values(values) as number[];
  if (xs.length === 0) return false;
  const hasZero = xs.some((v) => v === 0);
  const hasNonZero = xs.some((v) => v !== 0);
  return hasZero && hasNonZero;
}

/**
 * Connector-aware partial detection (CTO-107).
 *
 * Returns the subset of *enabled* layers that report zero — those are the real gaps. Layers the
 * tenant never enabled don't count: a tenant who only declared the LLM connector should never see
 * the banner for vector/tools/etc., because they were never expected to fire. With the empty
 * input (no enabled connectors declared) we return [], i.e. nothing partial — by design.
 */
export function zeroEnabledLayers<L extends string>(
  byLayer: Record<L, number>,
  enabled: readonly L[],
): L[] {
  return enabled.filter((l) => (byLayer[l] ?? 0) === 0);
}
