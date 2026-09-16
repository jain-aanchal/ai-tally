// SPDX-License-Identifier: Apache-2.0
// Shapes for the pre-deploy Estimate workflow (CTO-71/72/73), plus the sample fixture the demo
// path renders (CTO-128).
//
// CTO-298: every figure here is nullable now, because /estimate answered a real signed-in tenant
// with the fixture at the bottom of this file and called it their analysis: a $19,100/mo baseline,
// a 42% blow-up risk, an invented pull request and three invented cost drivers. The route serves
// the fixture only where sampleDataAllowed() permits it; every other caller gets
// EMPTY_PROJECTION, whose nulls the page renders as blanks carrying the reason.

import type { FirstEventStatus } from "./firstEvent";
import type { MicroUSD } from "./types";

/** Where a payload's figures came from. `none` is the honest empty answer for a real tenant. */
export type ReplaySource = "replay" | "mock" | "none";

/**
 * One side of the comparison.
 *
 * Both percentile-ish fields are nullable for the same CTO-298 reason: they were being manufactured
 * from numbers that do not contain them. `p99CostMicroUsd` was `monthlyCost * 1.4`, a fixture
 * multiplier wearing a percentile's name, and `meanLatencyMs` was fed straight from the replay
 * row's p50, which is a median relabelled as a mean. Neither is recoverable from what the replay
 * executor returns today, so both stay null until it returns a real distribution.
 */
export interface Figures {
  monthlyCostMicroUsd: MicroUSD | null;
  p99CostMicroUsd: MicroUSD | null;
  meanLatencyMs: number | null;
}

export interface Projection {
  workload: string | null;
  pr?: { repo: string; number: number; title: string } | null;
  current: Figures;
  proposed: Figures;
  /** Probability that p99 cost more than doubles under the change (0..1). The headline risk. */
  blowUpRisk: number | null;
  drivers: { delta: number; reason: string }[]; // delta in micro-USD/month
  sample: {
    /**
     * Replayed samples this projection used, or null when no replay was attempted.
     *
     * CTO-298 follow-up: this was a bare `number` and EMPTY_PROJECTION set it to 0, so the "samples
     * used" diagnostic printed a literal 0 on every path where nothing had been replayed. A count
     * nobody took is unknown, not zero. A replay that DID run and matched nothing still reports its
     * real 0, which is a measurement; the two are only tellable apart because this is nullable.
     */
    used: number | null;
    tailWeighted: number | null;
    pathologicalIncluded: number | null;
    ciHalfWidthPct: number | null; // on p99
  };
  /**
   * Minutes since the reconciler last trued-up the historical window this projection samples.
   * An estimate built on a stale baseline must not be presented as fresh (CTO-80). Sourced from the
   * real reconciliation_runs log on the live path (CTO-169); `null` when the reconciler has never
   * run / the source is unavailable, rendered as a blank rather than the fixture constant.
   */
  reconcilerLastRunMinutesAgo: number | null;
  /**
   * True only when the figures above are the fixture's.
   *
   * CTO-298: the page's SAMPLE DATA banner used to key on `current.monthlyCostMicroUsd === 0`, and
   * the fixture baseline is 19.1e9, so the one payload the banner existed to label was the one
   * payload that could never trigger it. Keying on the provenance itself makes the condition true
   * exactly when the data is synthetic.
   */
  synthetic: boolean;
  /**
   * What the first-event probe measured about this workspace: `connected` (a span exists),
   * `waiting` (the probe ran and found none), `unknown` (the probe could not run).
   *
   * CTO-298 follow-up, and the reason it exists. The page derived its empty state from
   * `current.monthlyCostMicroUsd === null`, and `current` is filled in by the fixture alone, so
   * that test was true for EVERY real tenant: a pilot with a live replay corpus was told, as a
   * measured fact, that no priced traffic had reached ai-tally, by a route that had queried neither
   * spend nor traffic. The empty state now keys on something actually measured, and `unknown` is
   * carried through rather than folded onto `waiting`, so a probe that could not run never reads as
   * a definite "nothing is here".
   */
  workspaceTraffic: FirstEventStatus;
}

/**
 * What-if projection returned by `POST /api/estimate` (CTO-128). `groundedSamples` carries how many
 * replayed samples actually grounded it; below the route's floor the proposed figures are null and
 * the page renders blanks rather than a forecast off noise.
 */
export interface WhatIfProjection extends Projection {
  candidate: { provider: string; model: string };
  systemPromptOverride?: string;
  /** Null when no replay ran at all (CTO-298 follow-up): never 0 standing in for an unknown. */
  groundedSamples: number | null;
  replay_source: ReplaySource;
}

export function pctDelta(cur: number | null, prop: number | null): number | null {
  // CTO-298: an unknown baseline yields an unknown delta. It cannot be treated as zero, because a
  // "0%" reads as "we measured no change" rather than "we have nothing to compare against".
  if (cur === null || prop === null) return null;
  if (cur === 0) return 0;
  return (prop - cur) / cur;
}

/**
 * The honest answer for a tenant with no replayed corpus behind this workload (CTO-298).
 *
 * Every figure is null rather than 0, and `drivers` is empty rather than the fixture's three: a
 * driver breakdown nobody computed is not a breakdown totalling zero.
 *
 * What this constant does NOT decide is whether the workspace is empty. It carries no measurement,
 * so `workspaceTraffic` defaults to `unknown` and the route overwrites it with what the first-event
 * probe found. Reading emptiness off these nulls is exactly the CTO-298 follow-up bug: they are
 * null on every real tenant's payload, corpus or no corpus.
 */
export const EMPTY_PROJECTION: Projection = {
  workload: null,
  pr: null,
  current: { monthlyCostMicroUsd: null, p99CostMicroUsd: null, meanLatencyMs: null },
  proposed: { monthlyCostMicroUsd: null, p99CostMicroUsd: null, meanLatencyMs: null },
  blowUpRisk: null,
  drivers: [],
  sample: { used: null, tailWeighted: null, pathologicalIncluded: null, ciHalfWidthPct: null },
  reconcilerLastRunMinutesAgo: null,
  synthetic: false,
  workspaceTraffic: "unknown",
};

/**
 * The demo storyline. `satisfies` rather than a type annotation so the literal figures stay
 * non-null for the fixture's own callers (lib/estimate.test.ts does arithmetic on them) while the
 * object still has to satisfy the nullable wire shape.
 *
 * CTO-298 deliberately keeps this: it is what the sample and demo path renders, behind
 * sampleDataAllowed() and behind the SAMPLE DATA banner. What changed is that it is no longer
 * reachable by a tenant who could mistake it for their own numbers.
 */
export const projection = {
  workload: "research_agent / production / last 30 days",
  pr: { repo: "jain-aanchal/ai-tally", number: 1284, title: "agent: add web_fetch retries + reranker step" },
  current: {
    monthlyCostMicroUsd: 19_100_000_000, // matches the research_agent line in cost.ts featureRows
    p99CostMicroUsd: 4_870_000,
    meanLatencyMs: 1400,
  },
  proposed: {
    monthlyCostMicroUsd: 33_240_000_000, // +74% projected
    p99CostMicroUsd: 11_580_000,
    meanLatencyMs: 2100,
  },
  blowUpRisk: 0.42,
  drivers: [
    { delta: 11_320_000_000, reason: "longer system prompt (4.2k → 6.8k tokens)" },
    { delta: 4_160_000_000, reason: "new tool call in 60% of paths" },
    { delta: -1_340_000_000, reason: "cached input recapture (Sonnet prompt caching)" },
  ],
  sample: {
    used: 180,
    tailWeighted: 140,
    pathologicalIncluded: 18,
    ciHalfWidthPct: 0.18,
  },
  reconcilerLastRunMinutesAgo: 18,
  synthetic: true,
  // The demo storyline has traffic behind it by construction, so the page renders the what-if body
  // (inside the SAMPLE DATA banner) rather than the new-workspace state.
  workspaceTraffic: "connected",
} satisfies Projection;
