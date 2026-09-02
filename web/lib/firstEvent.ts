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
