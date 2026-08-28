// SPDX-License-Identifier: Apache-2.0
// Types for the per-customer cost tab (workstream D of docs/cost-per-customer-plan.md).
//
// No mock data lives here on purpose. Every other lib module in this directory carries a canned
// fixture because its page shipped before the telemetry did; this one ships dark by design. Until a
// tenant instruments `account_id` (B2/B3) every span lands in the unattributed bucket, and a
// plausible-looking fake account list would be the single most misleading thing this page could
// show. The empty state (D5) is the answer to no data, not invented data.

import {
  allocateShared,
  type AllocationResult,
  type AllocationRule,
} from "./allocation";
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

/** Heaviest runs are capped for the same reason as features: a shortlist, not an audit trail. */
export const MAX_ACCOUNT_TOP_RUNS = 8;

/**
 * One agent run, costed at THIS account's spans only (CTO-190, plan D4).
 *
 * A trace can carry spans for more than one account: a batch job that serves several customers in
 * one run is a normal shape, and so is a run where only some steps were tagged. So the figure here
 * is the account's share of the run, not the run's total, and it will be smaller than the number
 * the same run shows on /agents whenever the run is shared. Reporting the run total on a
 * per-account page would double-count the shared part across every account it touched.
 */
export interface AccountRunCost {
  /** Trace id. Links to the existing /agents/runs/[runId] drill-down, which shows the WHOLE run. */
  runId: string;
  /** ServiceName, or `'untagged'` where the run carries none. Matches /agents. */
  agent: string;
  /** Cost of this account's spans in the run. See the note above: not the run's total. */
  accountCostMicroUsd: MicroUSD;
  /** Spans in the run attributed to this account, again not the run's total step count. */
  steps: number;
  /** Only success/failed are inferable from OTel StatusCode; `abandoned` is not tracked. */
  outcome: "success" | "failed";
}

export interface AccountDetail extends AccountCostRow {
  /** Heaviest features for this account, capped at {@link MAX_ACCOUNT_TOP_FEATURES}. */
  topFeatures: AccountFeatureCost[];
  /** One point per calendar day across the window, oldest first, gaps filled with zero. */
  trend: AccountTrendPoint[];
  /** Heaviest runs for this account, capped at {@link MAX_ACCOUNT_TOP_RUNS}. */
  topRuns: AccountRunCost[];
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

// --- Revenue and gross margin (CTO-197, plan E4) -------------------------------------------------
//
// The column that turns this tab from a cost report into a profitability view, and the one place on
// the page where a wrong number gets acted on: someone reads "this customer is unprofitable" and
// goes and reprices them. So both of the ways this figure can mislead are handled here rather than
// left to the JSX.
//
// 1. NULL IS NOT ZERO. `queryAccountRevenue` (lib/accountRevenue.ts) separates them deliberately.
//    An account whose only events are `count` engagement signals returns null, meaning we were
//    never told its revenue. A charge fully netted by a refund returns 0, which is a measurement.
//    Collapsing the two would either invent revenue for a customer we know nothing about, or claim
//    a paying customer generates none. So margin is null exactly when revenue is null, and 0
//    revenue produces a real margin of minus the cost.
//
// 2. THE MARGIN IS OVERSTATED, ALWAYS, IN V1. Cost here is direct cost only: compute and egress are
//    excluded (Decision 2 in docs/cost-per-customer-plan.md, roughly 47 percent of spend on the
//    current tenant). Every margin on this page is therefore too high by whatever share of that
//    account's real cost sits in those two layers. That caveat travels with the number, on the cell
//    itself, because a ranking that silently ignores half the cost base is worse than no ranking.

/**
 * Why every margin on this page reads high. Attached to each printed margin, not only to the page
 * header, because the header scrolls away and the number is what gets copied into a pricing
 * conversation.
 */
export const MARGIN_EXCLUDES_INFRA =
  "understated cost: compute and egress are excluded from every account, so this margin is too high by whatever share of this customer's cost sits in those layers";

/**
 * Ratio of direct cost to revenue below which the margin is arithmetically indistinguishable from
 * revenue itself.
 *
 * One percent. Under it, subtracting cost moves the figure by less than rounding does, so "margin"
 * is really just "revenue with a cost column that never arrived". That is the exact shape of the
 * current tenant: real uploaded revenue against near-zero attributed spend, which would otherwise
 * render as a flawless customer rather than as a customer we have barely measured.
 */
export const MIN_COST_TO_REVENUE_RATIO = 0.01;

/**
 * Spans of attributed cost an account needs before its margin is read as a cost measurement.
 *
 * Shares the floor `costPerUser` uses, for the same reason: below it the account's cost side is a
 * handful of spans, and a margin computed against it describes our instrumentation coverage rather
 * than the customer's economics.
 */
export const MIN_SPANS_FOR_MARGIN = MIN_SPANS_FOR_COST_PER_USER;

/** Revenue, margin, and everything the reader needs in order not to over-read them. */
export interface AccountMargin {
  /** Net revenue in micro-USD, or `null` for "we have not been told". Never 0 to mean unknown. */
  revenueMicroUsd: MicroUSD | null;
  /** Revenue minus direct cost. `null` exactly when `revenueMicroUsd` is null. */
  marginMicroUsd: MicroUSD | null;
  /** Why revenue and margin are blank. `null` when they are not. */
  reason: string | null;
  /**
   * Why a printed margin must not be read as this customer's true profitability. Never empty when a
   * margin prints: {@link MARGIN_EXCLUDES_INFRA} applies to every account in v1.
   */
  caveats: string[];
}

/**
 * Revenue and gross margin for one account row.
 *
 * `revenueMicroUsd` is what {@link revenueForAccount} returned: the account's net revenue, or null
 * for both "no row" and "no money-typed event". Those are the same statement to a reader, so they
 * get the same blank. `revenueUnavailable` is a third and different case: the revenue read itself
 * failed, so a blank here is not evidence that nothing is wired up, and the reason says so.
 */
export function accountMargin(
  row: AccountCostRow,
  revenueMicroUsd: MicroUSD | null,
  revenueUnavailable = false,
): AccountMargin {
  if (revenueMicroUsd === null) {
    return {
      revenueMicroUsd: null,
      marginMicroUsd: null,
      reason: revenueUnavailable
        ? "revenue could not be read for this window, so no margin can be computed. This blank is not evidence that no revenue source is wired"
        : "no revenue source wired for this account, so its revenue is unknown. An unknown is not zero, and a margin against an assumed zero would be invented",
      caveats: [],
    };
  }

  const caveats = [MARGIN_EXCLUDES_INFRA];
  // Two separate ways the cost side can be too thin to carry a margin, deliberately not merged: one
  // is about how little we measured, the other about how little what we measured amounts to.
  if (row.spanCount < MIN_SPANS_FOR_MARGIN) {
    caveats.push(
      `thin cost data: ${row.spanCount.toLocaleString()} attributed spans in the window, below the ${MIN_SPANS_FOR_MARGIN}-span floor, so this is closer to raw revenue than to a measured margin`,
    );
  }
  if (
    revenueMicroUsd > 0 &&
    row.directCostMicroUsd / revenueMicroUsd < MIN_COST_TO_REVENUE_RATIO
  ) {
    caveats.push(
      "attributed cost is under 1% of revenue, so subtracting it barely moves the figure. Read this as revenue with the cost side largely missing, not as a near-perfect margin",
    );
  }

  return {
    revenueMicroUsd,
    marginMicroUsd: revenueMicroUsd - row.directCostMicroUsd,
    reason: null,
    caveats,
  };
}

// --- Presentation helpers for the account detail view (CTO-190, plan D4) -------------------------

/** Characters in a stored account hash: HMAC-SHA256 rendered hex. */
export const ACCOUNT_HASH_CHARS = 64;

/**
 * Whether a URL segment is a well-formed account hash.
 *
 * The detail route's parameter is user-editable, so it is checked for shape before it reaches a
 * query. This is a shape test and nothing more: a well-formed hash that matches no rows is a
 * perfectly ordinary answer (an account with no spend in the window), and the page says so rather
 * than treating it as an error. Only a segment that could never have been a hash gets the
 * "that is not an account id" treatment.
 */
export function isAccountHash(segment: string): boolean {
  return new RegExp(`^[0-9a-f]{${ACCOUNT_HASH_CHARS}}$`).test(segment);
}

/**
 * What the Account column and the detail header both print for an account.
 *
 * One function so the two surfaces cannot drift: a reader who clicks "Acme Corp" in the table must
 * land on a page headed "Acme Corp", and a reader who clicks a shortened hash must land on the same
 * shortened hash. `undefined` label (no label set, or labels unavailable) falls back to the short
 * form; the full hash stays reachable through the copy control on both surfaces.
 */
export function accountDisplayName(accountIdHash: string, label: string | undefined): string {
  return label ?? shortenAccountHash(accountIdHash);
}

/**
 * Share of an account's direct spend sitting in one layer, or `null` when it has no direct spend.
 *
 * `null` rather than 0 for the same reason {@link unattributedShare} returns it: "0% of spend is
 * LLM" is a claim about a distribution that does not exist. The caller renders a blank.
 */
export function layerShare(row: AccountCostRow, layer: DirectLayer): number | null {
  if (row.directCostMicroUsd <= 0) return null;
  return row.byLayer[layer] / row.directCostMicroUsd;
}

/**
 * The trend's own total, for the chart to state beside the account total it is drawn under.
 *
 * These two are computed from different reads (a per-day group and a per-layer group), so they are
 * two chances to disagree, and the day-list-from-the-wrong-clock bug drops a day from the chart
 * while leaving it in the total. Exposing the chart's sum makes the disagreement visible instead of
 * silent, and the detail page asserts on it.
 */
export function trendTotal(trend: readonly AccountTrendPoint[]): MicroUSD {
  return trend.reduce((sum, p) => sum + p.directCostMicroUsd, 0);
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

// --- Allocated infrastructure cost (CTO-193, plan C2) --------------------------------------------
//
// This is where the excluded half of the bill stops being excluded. CTO-189 could only state the
// SIZE of what the table left out; with an allocation rule in force (CTO-192 for the arithmetic,
// C2 for the per-tenant rule) compute and egress can be attributed, and the table carries direct,
// allocated and total per account instead of a banner apologising for the gap.
//
// THE DECISION THIS SECTION ENCODES, and the most consequential one in the ticket: THE
// UNATTRIBUTED BUCKET PARTICIPATES IN ALLOCATION, as a first-class participant alongside the real
// accounts.
//
// It is a synthetic row, not a customer, so treating it as one deserves a defence. The defence is
// that every alternative is worse in the case that actually occurs. Shared infrastructure is caused
// by ALL traffic, and untagged traffic is traffic: on the current tenant it is over 99.99 percent
// of direct spend, so it is causing essentially all of the compute bill. Leaving the bucket out
// would divide the whole shared total across only the accounts that happen to be tagged, and hand
// three accounts with a few dollars of direct spend roughly fourteen thousand dollars of compute
// each. That figure is not merely unhelpful, it is false, and it is worst exactly when a tenant is
// earliest in rolling out `account_id` and least equipped to spot it. It also has a property no
// cost report may have: instrumenting one more account would halve every existing account's cost.
//
// With the bucket participating, the same tenant reads honestly. The unattributed row carries
// almost all of the shared cost, the tagged accounts carry a share proportional to what they
// actually used, and the page can say plainly that most infrastructure cost belongs to traffic that
// carries no account yet. The remedy that reading suggests, tag more spans, is the true one.
//
// The bucket keeps `unattributed: true` throughout, so no surface can rank it as a customer, and
// the page labels its share as belonging to untagged traffic rather than to anybody.

/** One table row once shared cost has been allocated. `total = direct + allocated`. */
export interface AllocatedAccountRow extends AccountCostRow {
  /** Estimated share of tenant-level compute and egress. NOT measured. */
  allocatedMicroUsd: MicroUSD;
  /** `directCostMicroUsd + allocatedMicroUsd`. Part measured, part estimated. */
  totalMicroUsd: MicroUSD;
}

export interface AllocatedAccountCosts {
  windowDays: number;
  /** The rule asked for. */
  rule: AllocationRule;
  /**
   * The rule actually applied. Differs from {@link AllocatedAccountCosts.rule} when pro rata had no
   * denominator and the engine fell back to an even split. The page names this one, because it is
   * the one that produced the numbers.
   */
  effectiveRule: AllocationRule;
  /** Real accounts, ranked by TOTAL cost, most expensive first. */
  accounts: AllocatedAccountRow[];
  /** The unattributed bucket with its allocated share. Never ranked among the accounts. */
  unattributed: AllocatedAccountRow;
  /** Compute plus egress for the window: the pot that was shared out. */
  sharedMicroUsd: MicroUSD;
  directTotalMicroUsd: MicroUSD;
  allocatedTotalMicroUsd: MicroUSD;
  /** `direct + shared`. What the rows must add up to, and what `/cost` reports for the window. */
  tenantTotalMicroUsd: MicroUSD;
}

/**
 * Allocate the window's compute and egress across every account plus the unattributed bucket.
 *
 * Returns `null` when the excluded total could not be read. That is not a zero: allocating nothing
 * would print a total equal to direct cost for every account, quietly restoring the understated
 * figures CTO-189 existed to flag with no banner left to warn about them. The caller renders the
 * unreadable case explicitly instead.
 *
 * RECONCILIATION, the acceptance test of this ticket:
 *
 *     sum(accounts.total) + unattributed.total === tenantTotalMicroUsd === direct + compute + egress
 *
 * exactly, in integer micro-USD, which is the same total `/cost` reports for the same window. The
 * guarantee itself comes from `allocateShared`. The job here is to feed it EVERY row holding direct
 * spend (which is why the unattributed bucket has to go in) and to add nothing to its output.
 */
export function allocateAccountCosts(
  costs: AccountCosts,
  excluded: ExcludedInfraCost | null,
  rule: AllocationRule,
): AllocatedAccountCosts | null {
  if (excluded === null) return null;

  // The bucket goes in last, so a remainder tie between it and a real account breaks toward the
  // account: the engine breaks ties by input order. The stake is one micro-USD, so this is
  // cosmetic, and it is ordered deliberately anyway because "why did the synthetic row get the
  // spare unit" is a question someone eventually asks of a reconciliation report.
  const participants = [...costs.accounts, costs.unattributed];
  const result: AllocationResult = allocateShared(
    participants.map((row) => ({
      accountId: row.accountIdHash,
      directMicroUsd: row.directCostMicroUsd,
    })),
    excluded.totalMicroUsd,
    rule,
  );

  const allocatedRows: AllocatedAccountRow[] = participants.map((row, i) => ({
    ...row,
    allocatedMicroUsd: result.accounts[i].allocatedMicroUsd,
    totalMicroUsd: result.accounts[i].totalMicroUsd,
  }));
  const unattributed = allocatedRows[allocatedRows.length - 1];
  const accounts = allocatedRows.slice(0, -1);
  // Ranked on TOTAL, not direct. With roughly half the bill allocated, the most expensive customer
  // by direct spend is not necessarily the most expensive customer.
  accounts.sort((a, b) => b.totalMicroUsd - a.totalMicroUsd);

  return {
    windowDays: costs.windowDays,
    rule: result.rule,
    effectiveRule: result.effectiveRule,
    accounts,
    unattributed,
    sharedMicroUsd: excluded.totalMicroUsd,
    directTotalMicroUsd: result.directTotalMicroUsd,
    allocatedTotalMicroUsd: result.allocatedTotalMicroUsd,
    tenantTotalMicroUsd: result.tenantTotalMicroUsd,
  };
}

/**
 * The sum the reconciliation line prints: every row's total, bucket included.
 *
 * Summed from the rendered rows rather than read back off `tenantTotalMicroUsd`, so the check is a
 * real check. Reading the claimed total out of the same object would assert nothing about the
 * numbers actually on screen.
 */
export function allocatedRowsTotal(allocated: AllocatedAccountCosts): MicroUSD {
  return (
    allocated.accounts.reduce((sum, r) => sum + r.totalMicroUsd, 0) +
    allocated.unattributed.totalMicroUsd
  );
}

/** Short display name for a rule. Shown wherever an allocated figure is. */
export const ALLOCATION_RULE_LABELS: Record<AllocationRule, string> = {
  pro_rata_direct: "pro rata on direct spend",
  even_split: "even split across accounts",
};

/** One sentence saying what the rule did, for the reader who wants to check the arithmetic. */
export const ALLOCATION_RULE_DESCRIPTIONS: Record<AllocationRule, string> = {
  pro_rata_direct:
    "each account's share of compute and egress is proportional to its own direct spend",
  even_split: "compute and egress are split equally across every account and the untagged bucket",
};

// --- Which of the tab's states to render (CTO-191, plan D5) --------------------------------------

/**
 * Above this share, the unattributed bucket is the story rather than a footnote.
 *
 * Lives here rather than in page.tsx because the state machine below is the thing worth testing,
 * and a threshold the test cannot see is a threshold the test cannot pin.
 */
export const MAJORITY_UNATTRIBUTED = 0.5;

/**
 * The three honest readings of a successful query.
 *
 *   - `onboarding`: not one span in the window carried an account, so there is no breakdown to
 *     show and the reader has almost certainly never seen this page. Explain the page, then say
 *     how to switch it on.
 *   - `partial`: some accounts exist but most spend still has none, so the ranking is real and
 *     incomplete at the same time. Show it, and say what it is missing.
 *   - `attributed`: most spend carries an account. The table speaks for itself.
 *
 * A failed query is deliberately NOT a state here. "ClickHouse is unreachable" is a different fact
 * from "you have not instrumented this yet", and answering an outage with an onboarding pitch would
 * blame the reader for our own broken dependency. page.tsx branches on `costs === null` first, and
 * this function is only ever reached with data in hand.
 */
export type AccountsView = "onboarding" | "partial" | "attributed";

export function accountsView(costs: AccountCosts): AccountsView {
  // Keyed on "are there any accounts", not on "is spend zero". A tenant can have real accounts and
  // no spend in the window, which is a quiet week rather than an uninstrumented one, and telling it
  // to go install the SDK it already installed would be wrong.
  if (costs.accounts.length === 0) return "onboarding";
  const share = unattributedShare(costs);
  if (share !== null && share >= MAJORITY_UNATTRIBUTED) return "partial";
  return "attributed";
}
