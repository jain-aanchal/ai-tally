// SPDX-License-Identifier: Apache-2.0
// Types for the per-customer cost tab (workstream D of docs/cost-per-customer-plan.md).
//
// No mock data lives here on purpose. Every other lib module in this directory carries a canned
// fixture because its page shipped before the telemetry did; this one ships dark by design. Until a
// tenant instruments `account_id` (B2/B3) every span lands in the unattributed bucket, and a
// plausible-looking fake account list would be the single most misleading thing this page could
// show. The empty state (D5) is the answer to no data, not invented data.

import type { MicroUSD, SpendByLayer } from "./types";

/**
 * The layers that are directly attributable to one account.
 *
 * Compute and egress are deliberately absent. They are tenant-level infrastructure spend that no
 * span carries an account for, so splitting them per customer would mean inventing an allocation
 * rule; that rule is workstream C and the separately-surfaced excluded total is CTO-189. Keeping
 * this a narrower type than {@link SpendByLayer} rather than a six-key object with two zeroes is
 * what stops "add up every layer" from quietly folding infrastructure into a customer's bill.
 */
export const DIRECT_LAYERS = ["llm", "tools", "vector", "embeddings"] as const;
export type DirectLayer = (typeof DIRECT_LAYERS)[number];
export type DirectSpendByLayer = Pick<SpendByLayer, DirectLayer>;

/** The account id of the unattributed bucket: spans that carry no `account_id` at all. */
export const UNATTRIBUTED_ACCOUNT = "";

export function zeroDirectLayers(): DirectSpendByLayer {
  return { llm: 0, tools: 0, vector: 0, embeddings: 0 };
}

export function totalDirect(byLayer: DirectSpendByLayer): MicroUSD {
  return DIRECT_LAYERS.reduce((sum, l) => sum + byLayer[l], 0);
}

/** One row of the per-customer table: a real account, or the unattributed bucket. */
export interface AccountCostRow {
  /** 64-char hex hash, or `''` for the unattributed bucket. Never a plaintext customer id. */
  accountIdHash: string;
  /**
   * True for the one synthetic row covering spans with no account. It is NOT a customer and must
   * never be ranked alongside real accounts or labelled as one.
   */
  unattributed: boolean;
  byLayer: DirectSpendByLayer;
  directCostMicroUsd: MicroUSD;
  /**
   * Distinct users seen against this account in the window, from the rollup's `uniq` state. It is
   * an approximation (HyperLogLog), which is fine for a "how many people is this?" column and is
   * why it must never be used as a divisor for a headline per-user figure.
   */
  distinctUsers: number;
  spanCount: number;
}

/** A row with nothing in it. Used for the unattributed bucket when the window holds no such spans. */
export function emptyAccountRow(accountIdHash: string, unattributed: boolean): AccountCostRow {
  return {
    accountIdHash,
    unattributed,
    byLayer: zeroDirectLayers(),
    directCostMicroUsd: 0,
    distinctUsers: 0,
    spanCount: 0,
  };
}

export interface AccountCosts {
  /** Calendar days covered, inclusive of today. Matches the window Home and /cost use. */
  windowDays: number;
  /** Real accounts only, ranked by direct cost, most expensive first. */
  accounts: AccountCostRow[];
  /** Always present, even at zero, so the page can state the unattributed share unconditionally. */
  unattributed: AccountCostRow;
  /**
   * `accounts` plus `unattributed`. This is the reconciliation figure: it must equal the tenant's
   * direct spend for the same window, or this tab contradicts /cost.
   */
  totalDirectMicroUsd: MicroUSD;
}

/** Top features are capped: this is a "where does this customer's money go" list, not an audit. */
export const MAX_ACCOUNT_TOP_FEATURES = 5;

export interface AccountFeatureCost {
  feature: string;
  directCostMicroUsd: MicroUSD;
  spanCount: number;
}

export interface AccountTrendPoint {
  date: string; // ISO yyyy-mm-dd
  directCostMicroUsd: MicroUSD;
}

export interface AccountDetail extends AccountCostRow {
  /** Heaviest features for this account, capped at {@link MAX_ACCOUNT_TOP_FEATURES}. */
  topFeatures: AccountFeatureCost[];
  /** One point per calendar day across the window, oldest first, gaps filled with zero. */
  trend: AccountTrendPoint[];
}

// --- Presentation helpers for the /cost-per-customer tab (CTO-188, plan D2) ----------------------
//
// Pure functions, no I/O, so the honesty rules the page depends on are unit-testable rather than
// buried in JSX. Every one of them exists to stop the tab printing a number it cannot stand behind.

/**
 * Spans an account needs in the window before a per-user figure is printed.
 *
 * Follows the `/compare` precedent ("needs ≥50 spans in 7d", and MIN_SPANS_FOR_LATENCY_ERROR in
 * clickhouse.ts). Cost per user is a ratio of two small numbers on a quiet account: one expensive
 * retry against a single user reads as an eye-watering per-seat cost, and a reader cannot tell that
 * apart from a real one. Below the floor the cell is blank with the reason attached.
 */
export const MIN_SPANS_FOR_COST_PER_USER = 50;

/** A value the tab either has, or does not have and can say why. `micro` and `reason` are exclusive. */
export interface HonestMicro {
  micro: MicroUSD | null;
  reason: string | null;
}

/**
 * Direct cost divided by distinct users, or a blank carrying its reason.
 *
 * Two separate blanks, deliberately not collapsed into one: too few spans to trust the ratio, and
 * no users to divide by at all. Those are different facts about the account, and a reader chasing
 * an empty cell wants the one that applies.
 *
 * `distinctUsers` is a HyperLogLog approximation (see {@link AccountCostRow}), which is fine as a
 * divisor for one account's row and is why this figure is never summed into a headline.
 */
export function costPerUser(row: AccountCostRow): HonestMicro {
  if (row.distinctUsers <= 0) {
    return {
      micro: null,
      reason: "no distinct users recorded for this account, so there is nothing to divide by",
    };
  }
  if (row.spanCount < MIN_SPANS_FOR_COST_PER_USER) {
    return {
      micro: null,
      reason: `needs ≥${MIN_SPANS_FOR_COST_PER_USER} spans in the window; this account has ${row.spanCount.toLocaleString()}`,
    };
  }
  return { micro: Math.round(row.directCostMicroUsd / row.distinctUsers), reason: null };
}

/** Leading hex characters shown when an account carries no label. */
export const SHORT_HASH_CHARS = 12;

/**
 * The shortened hash shown in the Account column when no label exists.
 *
 * Truncated for width only. The full hash stays available on hover and through the copy control,
 * because the short form is not an identifier: it is not what the lookup endpoint returns and it is
 * not what a label write accepts.
 */
export function shortenAccountHash(accountIdHash: string): string {
  if (accountIdHash.length <= SHORT_HASH_CHARS) return accountIdHash;
  return `${accountIdHash.slice(0, SHORT_HASH_CHARS)}…`;
}

/**
 * Share of direct spend carrying no account, as a fraction, or `null` when the tenant recorded no
 * direct spend at all in the window.
 *
 * `null` rather than 0: with nothing to attribute, "0% unattributed" is a claim of perfect coverage
 * and the exact opposite of what is true. This is the page's honesty valve, so it fails to a blank
 * rather than to a flattering number.
 */
export function unattributedShare(costs: AccountCosts): number | null {
  if (costs.totalDirectMicroUsd <= 0) return null;
  return costs.unattributed.directCostMicroUsd / costs.totalDirectMicroUsd;
}

/** Decimal places the unattributed share is printed to. */
export const SHARE_DIGITS = 1;

/**
 * The unattributed share as text, with the rounding boundaries called out.
 *
 * A share of 0.99999 rounds to "100.0%", and printing that beside a table of three real accounts is
 * a small lie in both directions: it says nothing is attributed while something plainly is. The
 * same trap sits at the other end, where a genuine sliver rounds to "0.0%" and reads as perfect
 * coverage. So the two boundaries print as ">99.9%" and "<0.1%", and only an exact 1 or 0 gets the
 * round number. This is the page's headline honesty figure; it is the last place to let a rounding
 * rule overstate the answer.
 */
export function formatShare(share: number): string {
  if (share >= 1) return "100.0%";
  if (share <= 0) return "0.0%";
  const rounded = (share * 100).toFixed(SHARE_DIGITS);
  if (Number(rounded) >= 100) return ">99.9%";
  if (Number(rounded) <= 0) return "<0.1%";
  return `${rounded}%`;
}

/** Direct spend that IS attributed to an account: the total less the unattributed bucket. */
export function attributedSpend(costs: AccountCosts): MicroUSD {
  return costs.totalDirectMicroUsd - costs.unattributed.directCostMicroUsd;
}

// --- Excluded infrastructure cost (CTO-189, plan D3) ---------------------------------------------

/**
 * Compute and egress over the same window the per-account table covers.
 *
 * These are the two layers {@link DIRECT_LAYERS} deliberately leaves out. They arrive from the
 * cloud billing connectors as tenant-level daily totals that carry no account, so the tab cannot
 * split them per customer without an allocation rule, and that rule is workstream C (CTO-192/193).
 * Carrying the figure here is what lets the page state the SIZE of what it omits rather than a
 * vague "some costs are excluded": on current data the omission is roughly half of all spend, and
 * an account figure that quietly understates true cost by half is the exact failure this product
 * exists to fix.
 */
export interface ExcludedInfraCost {
  /** Calendar days covered. Must match {@link AccountCosts.windowDays} or the share is meaningless. */
  windowDays: number;
  computeMicroUsd: MicroUSD;
  egressMicroUsd: MicroUSD;
  /** `compute + egress`, summed from the same rounded parts so it cannot contradict them. */
  totalMicroUsd: MicroUSD;
}

/**
 * Excluded spend as a share of ALL spend in the window, or `null` when there is nothing to divide.
 *
 * The denominator is direct plus excluded, i.e. the tenant's whole bill for the window, because the
 * sentence this feeds ("excludes $X, N% of spend") is only meaningful against the total. Dividing
 * by direct spend alone would print a share above 100 percent the moment infrastructure outweighs
 * the model bill, which on a compute-heavy tenant it does.
 *
 * Both inputs are read over the same window from the same rollup under complementary filters, so
 * they add to the tenant total exactly and this share reconciles with /cost.
 */
export function excludedShare(excluded: ExcludedInfraCost, costs: AccountCosts): number | null {
  const all = costs.totalDirectMicroUsd + excluded.totalMicroUsd;
  if (all <= 0) return null;
  return excluded.totalMicroUsd / all;
}
