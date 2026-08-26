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
