// SPDX-License-Identifier: Apache-2.0
// First-data onboarding status (Initiative 2, §9). The onboarding panel flips from "waiting" to
// "connected" when the first span for the tenant lands, and reports the honest state only: it never
// claims data that is not there.
//
// Three states, deliberately: a ClickHouse existence probe can come back "yes a row exists"
// (connected), "no rows yet" (waiting), or "we could not reach ClickHouse" (unknown). The last is
// NOT collapsed into "waiting": that would fabricate a definite negative from an actual absence of
// knowledge, against "honest under uncertainty" (CLAUDE.md). The pure mapper here is unit-tested;
// the live probe that produces its input lives in clickhouse.ts.

export type FirstEventStatus = "connected" | "waiting" | "unknown";

/**
 * Map a probe result to a status. `true` = a row exists (connected); `false` = the probe ran and
 * found none (waiting); `null` = the probe could not run (unknown), never a fabricated "waiting".
 */
export function firstEventStatus(seen: boolean | null): FirstEventStatus {
  if (seen === null) return "unknown";
  return seen ? "connected" : "waiting";
}

// --------------------------------------------------------------------------------------------
// Per-layer coverage (CTO-261, onboarding-agent §7).
//
// The binary status above answers "are you connected" and stops there, so a developer who wired the
// LLM one-liner sees a green tick while tools, vector, embeddings and per-customer attribution are
// all still dark. The gateway's per-layer probe widens it; this is the client-side view of that
// answer. `firstEventStatus` and its three states are untouched, because the connect panel still
// asks exactly the binary question and its callers should not have to care about layers.
//
// A dark layer splits in two, which is the distinction the whole panel exists to draw: a layer the
// onboarding agent wired but the app has not exercised yet ("wired, awaiting first event") is not
// the same news as a layer nothing instruments. And neither is `unknown`, which means the probe did
// not run. `covered` requires a proving span and is not reachable any other way, here or in the
// gateway.
// --------------------------------------------------------------------------------------------

/** The five layers §7 reports on, in the order a developer wires them. */
export const COVERAGE_LAYERS = ["llm", "tools", "vector", "embeddings", "account"] as const;
export type CoverageLayer = (typeof COVERAGE_LAYERS)[number];

export type LayerCoverageState = "covered" | "awaiting_first_event" | "not_wired" | "unknown";

export interface LayerCoverage {
  layer: CoverageLayer;
  state: LayerCoverageState;
  /** Why this layer is in this state. Always present: a dark or unknown layer is never unexplained. */
  reason: string;
  /** Spans proving the layer, or null when we could not count them. Never 0 standing in for null. */
  provingSpans: number | null;
}

/** Human labels for the layer names the wire speaks. */
export const COVERAGE_LAYER_LABELS: Record<CoverageLayer, string> = {
  llm: "LLM calls",
  tools: "Tool calls",
  vector: "Vector search",
  embeddings: "Embeddings",
  account: "Per-customer attribution",
};

/**
 * Map one layer's raw probe result to a state.
 *
 * `provingSpans` is the count of spans that prove the layer, or null when the probe could not run.
 * `wired` is the onboarding agent's claim that it instrumented this layer, which only ever softens
 * the wording of a dark layer. It cannot produce coverage: the only route to `covered` is a
 * positive span count, so a claim without a span still reads as awaiting.
 */
export function layerCoverageState(
  provingSpans: number | null,
  wired = false,
): LayerCoverageState {
  if (provingSpans === null) return "unknown";
  if (provingSpans > 0) return "covered";
  return wired ? "awaiting_first_event" : "not_wired";
}

/** Whether a state means "we have proof", for a caller counting covered layers. */
export function isCovered(state: LayerCoverageState): boolean {
  return state === "covered";
}

const _STATES: readonly LayerCoverageState[] = [
  "covered",
  "awaiting_first_event",
  "not_wired",
  "unknown",
];

const UNREADABLE =
  "the coverage probe did not return a usable answer for this layer, so we cannot tell whether it is flowing";

/** An all-unknown report, for when the probe itself could not be reached. `reason` says why. */
export function unknownCoverage(reason: string): LayerCoverage[] {
  return COVERAGE_LAYERS.map((layer) => ({
    layer,
    state: "unknown" as const,
    reason,
    provingSpans: null,
  }));
}

/**
 * Parse the gateway's coverage payload into the panel's shape, defensively.
 *
 * Defensive because this is the last gate before a claim reaches a developer's screen. Anything the
 * gateway sends that does not carry BOTH a `covered` state and a positive proving-span count is
 * downgraded to `unknown` with a reason rather than trusted, so no wire-shape drift, no partial
 * deploy and no future refactor can light a layer green without evidence behind it. A missing layer
 * is reported unknown too, never silently dropped from the panel.
 */
export function parseCoverage(raw: unknown, wired: readonly string[] = []): LayerCoverage[] {
  const rows = new Map<string, Record<string, unknown>>();
  const list = (raw as { layers?: unknown } | null)?.layers;
  if (Array.isArray(list)) {
    for (const row of list) {
      if (row && typeof row === "object" && typeof (row as { layer?: unknown }).layer === "string") {
        rows.set((row as { layer: string }).layer, row as Record<string, unknown>);
      }
    }
  }

  return COVERAGE_LAYERS.map((layer) => {
    const row = rows.get(layer);
    if (!row) {
      return { layer, state: "unknown" as const, reason: UNREADABLE, provingSpans: null };
    }
    const spansRaw = row.proving_spans;
    const spans =
      typeof spansRaw === "number" && Number.isFinite(spansRaw) && spansRaw >= 0
        ? Math.floor(spansRaw)
        : null;
    const reason = typeof row.reason === "string" && row.reason.trim() ? row.reason : UNREADABLE;
    const claimed = row.state;
    if (
      typeof claimed !== "string" ||
      !(_STATES as readonly string[]).includes(claimed) ||
      claimed === "unknown"
    ) {
      return { layer, state: "unknown" as const, reason, provingSpans: null };
    }
    // The evidence gate, and the reason this re-derives rather than trusting the wire: the state
    // shown comes from the span count, so `covered` is structurally unreachable without one. A
    // payload that claims coverage with nothing behind it reads as unknown, not as a green tick.
    const state = layerCoverageState(
      spans,
      claimed === "awaiting_first_event" || wired.includes(layer),
    );
    if (claimed === "covered" && state !== "covered") {
      return {
        layer,
        state: "unknown" as const,
        reason:
          "the probe reported this layer covered but returned no span to prove it, so we are not claiming it",
        provingSpans: null,
      };
    }
    return { layer, state, reason, provingSpans: spans };
  });
}
