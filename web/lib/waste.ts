// SPDX-License-Identifier: Apache-2.0
// Waste-detection framework and types (CTO-228, epic CTO-227). This is the FOUNDATION that every
// detector builds on; it lands before any detector so their shapes and the roll-up are fixed once.
//
// The whole feature answers "where is this tenant paying for AI that returns nothing" -- money spent
// with no measured value, duplicated work, an over-sized model, and so on. A detector inspects one
// slice of telemetry and emits zero or more findings; this module only defines what a finding IS and
// how a set of findings rolls up into a report. It is deliberately PURE: no queries, no React, no
// ClickHouse, no clock, no randomness, so the roll-up is trivially testable and deterministic.
//
// The honesty posture (see CLAUDE.md and components/HonestValue.tsx) is load-bearing here: a finding
// can be real and still be unable to bound the dollars it would recover. That case is `null`, never
// `0`, all the way through the report, so downstream renders a blank with a reason rather than a
// fabricated recoverable of zero.

import type { MicroUSD } from "@/lib/types";

/**
 * The kinds of waste the epic detects. One string per detector family; the union is closed so a new
 * category is a deliberate, reviewed addition (it forces every exhaustive switch and the `byCategory`
 * map to account for it).
 */
export type WasteCategory =
  | "paid_for_nothing"
  | "duplicated_work"
  | "wrong_sized_model"
  | "no_measured_return"
  | "structural_inefficiency";

/** Every category, in a stable order, so `byCategory` and any category iteration are deterministic. */
export const WASTE_CATEGORIES: readonly WasteCategory[] = [
  "paid_for_nothing",
  "duplicated_work",
  "wrong_sized_model",
  "no_measured_return",
  "structural_inefficiency",
];

/** How much we trust a finding. Drives sort/emphasis downstream, not the dollar math. */
export type WasteConfidence = "high" | "medium" | "low";

/** What a finding is scoped to: the dimension whose value (`scopeValue`) the waste is attributed to. */
export type WasteScopeKind = "feature" | "agent" | "model" | "layer" | "account";

/**
 * One unit of detected waste.
 *
 * `recoverableMicroUsd` is nullable ON PURPOSE (CTO-227 honesty): a finding can be real and still be
 * unable to put a defensible dollar bound on what stopping the waste would recover. That is `null`,
 * which renders a blank downstream, NOT `0` -- a guessed zero would understate recoverable spend and
 * read as "nothing to save here", the opposite of the truth. `windowSpendMicroUsd` is always known
 * (it is the observed spend on the scope over the window), so it is not nullable.
 */
export interface WasteFinding {
  category: WasteCategory;
  scopeKind: WasteScopeKind;
  scopeValue: string;
  /** Bounded recoverable spend, or `null` when the finding cannot defensibly bound the dollars. */
  recoverableMicroUsd: MicroUSD | null;
  /** Observed spend on the scope over the window. Always known. Integer micro-USD. */
  windowSpendMicroUsd: MicroUSD;
  confidence: WasteConfidence;
  title: string;
  reason: string;
  /** Free-form supporting counts/labels for the detail view. Values are display-ready strings/numbers. */
  evidence: Record<string, string | number>;
  /** Deep link into the drill-down for this finding, or `null` when there is no meaningful target. */
  drillHref: string | null;
}

/**
 * The rolled-up view a report surface consumes.
 *
 * `totalRecoverableMicroUsd` sums ONLY the findings that could bound their dollars, and is `null`
 * when NOT ONE finding could (an honest blank, never `0`). `byCategory` follows the same rule
 * per category: see {@link aggregateWaste} for the exact convention, documented there and enforced
 * by the tests. `unavailable` is a reason string carried by the endpoint when the report could not
 * be produced at all (e.g. the underlying query failed); the pure aggregation always sets it `null`.
 */
export interface WasteReport {
  findings: WasteFinding[];
  totalRecoverableMicroUsd: MicroUSD | null;
  byCategory: Record<WasteCategory, MicroUSD | null>;
  generatedForWindowDays: number;
  unavailable: string | null;
}

/**
 * A detector: given its own input, yields zero or more findings. Each detector defines its concrete
 * input in a LATER ticket, so the alias is intentionally permissive (a generic defaulting to
 * `unknown`) rather than a committed signature -- it documents the shape without constraining it.
 */
export type Detector<TInput = unknown> = (input: TInput) => WasteFinding[];

/** Identity of a finding for de-duplication: same category + scope + title is the same finding. */
function findingKey(f: WasteFinding): string {
  // JSON.stringify of a fixed-order tuple gives an unambiguous key even when a value contains the
  // separator we would otherwise join on. Order matters and is fixed here.
  return JSON.stringify([f.category, f.scopeKind, f.scopeValue, f.title]);
}

/**
 * Roll a flat list of findings into a {@link WasteReport}. PURE and deterministic.
 *
 * Steps:
 *   1. De-duplicate: findings identical on (category, scopeKind, scopeValue, title) collapse to the
 *      first occurrence. Detectors run independently and can legitimately surface the same waste.
 *   2. Sort by `recoverableMicroUsd` DESCENDING, with `null` (unbounded) findings sorted LAST, so the
 *      biggest bounded opportunities lead and the un-quantifiable ones trail. Ties preserve input
 *      order (stable), keeping the output deterministic.
 *   3. `totalRecoverableMicroUsd` = sum of the non-null recoverables, or `null` when there are zero
 *      non-null recoverables (an honest blank, never `0`).
 *   4. `byCategory[c]` = sum of the non-null recoverables of category `c`'s findings; `null` if `c`
 *      had findings but none were bounded; and ALSO `null` if `c` had no findings at all. We always
 *      populate every category key (never absent) and use `null` for "no bounded dollars to show",
 *      whether that is because the category was empty or because none of its findings were bounded.
 *      A blank is honest for both, and a total-less-than-sum-of-parts never appears.
 *   5. `unavailable` is always `null` here; only the endpoint sets it, on a failure to produce.
 */
export function aggregateWaste(findings: WasteFinding[], windowDays: number): WasteReport {
  // 1. De-dupe, keeping first occurrence and input order.
  const seen = new Set<string>();
  const deduped: WasteFinding[] = [];
  for (const f of findings) {
    const key = findingKey(f);
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(f);
  }

  // 2. Stable sort: bounded findings by recoverable descending, then all unbounded (null) findings.
  // Array.prototype.sort is stable in modern JS, so equal keys keep their de-duped input order.
  const sorted = deduped.slice().sort((a, b) => {
    const an = a.recoverableMicroUsd;
    const bn = b.recoverableMicroUsd;
    if (an === null && bn === null) return 0;
    if (an === null) return 1; // a (null) sorts after b
    if (bn === null) return -1; // b (null) sorts after a
    return bn - an; // both bounded: larger recoverable first
  });

  // 3 & 4. Totals. Track whether any bounded finding existed, so "no bounded dollars" is null not 0.
  const byCategory = {} as Record<WasteCategory, MicroUSD | null>;
  for (const c of WASTE_CATEGORIES) byCategory[c] = null;

  let total = 0;
  let anyBounded = false;
  for (const f of sorted) {
    if (f.recoverableMicroUsd === null) continue;
    anyBounded = true;
    total += f.recoverableMicroUsd;
    const prev = byCategory[f.category];
    byCategory[f.category] = (prev ?? 0) + f.recoverableMicroUsd;
  }

  return {
    findings: sorted,
    totalRecoverableMicroUsd: anyBounded ? total : null,
    byCategory,
    generatedForWindowDays: windowDays,
    unavailable: null,
  };
}
