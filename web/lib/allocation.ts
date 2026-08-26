// SPDX-License-Identifier: Apache-2.0
// Shared-cost allocation engine (CTO-192). Pure functions, no I/O.
//
// Direct spend (LLM, tools, vector, embeddings) carries an account on the span, so it can be
// attributed. Compute and egress cannot: they arrive from the cloud billing connectors as one
// synthetic span per provider per day at TENANT level, with no per-request detail. On current data
// that is about 47 percent of the bill, so an account figure that ignores it understates the true
// cost of every account by roughly half. Allocation is how the other half gets attributed.
//
// An allocated number is not a measured one, and the UI is expected to show direct and allocated as
// separate columns with the rule named on screen (scope Decision 3). This module deliberately
// returns both parts rather than one fused total, so a caller cannot present an estimate as a
// measurement by accident.
//
// THE INVARIANT, and the reason this is its own module with its own tests: the allocated shares plus
// whatever could not be allocated sum EXACTLY to the shared total, and direct plus allocated sums
// exactly to the tenant total. If per-account cost does not reconcile with what `/cost` reports, the
// feature loses trust the first time someone adds up a column.
//
// Money is integer micro-USD throughout (mirrors `unitEconomics.ts` and `tally.pricing`); never
// float dollars. The division is done with BigInt because a tenant total times an account weight
// overflows the 2^53 exact-integer range long before the bill itself gets large, and a float
// rounding error here is precisely the bug the invariant exists to catch.

/** How a tenant's shared infra cost is split across its accounts. */
export type AllocationRule = "pro_rata_direct" | "even_split";

/** All supported rules, in recommendation order. Useful for a config surface (plan item C2). */
export const ALLOCATION_RULES: readonly AllocationRule[] = [
  "pro_rata_direct",
  "even_split",
] as const;

/** The rule that ships as the default: infra broadly scales with model usage. */
export const DEFAULT_ALLOCATION_RULE: AllocationRule = "pro_rata_direct";

/** One account's directly attributable spend for the window. */
export interface AccountDirectSpend {
  /** `AccountIdHash`, or any stable per-account key. Must be unique within the input. */
  accountId: string;
  /** Measured direct spend, integer micro-USD. */
  directMicroUsd: number;
}

/** One account's allocation. `total = direct + allocated`, all integer micro-USD. */
export interface AccountAllocation {
  accountId: string;
  /** Measured. */
  directMicroUsd: number;
  /** Estimated share of the tenant-level shared total. */
  allocatedMicroUsd: number;
  /** `directMicroUsd + allocatedMicroUsd`. Part measured, part estimated. */
  totalMicroUsd: number;
}

export interface AllocationResult {
  /** The rule that was asked for. */
  rule: AllocationRule;
  /**
   * The rule actually applied. Differs from `rule` when pro rata had no denominator (every account
   * at zero direct spend) and fell back to an even split. Surface this: a reader comparing two
   * accounts deserves to know the named rule did not apply.
   */
  effectiveRule: AllocationRule;
  /** One entry per input account, in input order. */
  accounts: AccountAllocation[];
  /** The tenant-level shared total that was fed in. */
  sharedMicroUsd: number;
  /** Sum of `allocatedMicroUsd` across accounts. */
  allocatedTotalMicroUsd: number;
  /**
   * Shared cost with nowhere to go, which happens only when there are no accounts. Non-zero here
   * means the page must show an unallocated bucket rather than quietly dropping the money.
   */
  unallocatedMicroUsd: number;
  /** Sum of `directMicroUsd` across accounts. */
  directTotalMicroUsd: number;
  /** `directTotal + shared`. What the per-account rows must add up to. */
  tenantTotalMicroUsd: number;
}

/**
 * Allocate a tenant-level shared total across accounts.
 *
 * Guarantees, for every rule and every input:
 *   1. `sum(allocated) + unallocated === sharedMicroUsd`
 *   2. `sum(direct) + sum(allocated) + unallocated === tenantTotalMicroUsd`
 *   3. every returned amount is a safe integer
 *
 * Rounding uses largest-remainder: each account gets the floor of its exact share, and the leftover
 * micro-USD go one each to the largest remainders, ties broken by input order. Every unit lands
 * somewhere and no unit lands twice, which is what makes guarantee 1 hold on totals that do not
 * divide evenly. Deterministic, so two renders of the same window never disagree.
 *
 * Edge cases, decided here rather than left to the caller:
 *
 *   * **No accounts.** Nothing to allocate to. The whole shared total comes back as
 *     `unallocatedMicroUsd` rather than vanishing.
 *   * **Every account at zero direct spend.** Pro rata has no denominator. Rather than return zeros
 *     (which would silently drop the shared half of the bill) or divide by zero, this falls back to
 *     an even split and says so in `effectiveRule`. An account with no LLM spend in the window still
 *     consumed infra.
 *   * **Zero shared cost.** Every allocation is zero. No division happens at all.
 *   * **Negative direct spend** (a credit or a corrected bill) contributes zero pro-rata weight: a
 *     refund should not earn an account a negative share of infra it still used. The negative value
 *     is preserved untouched in `directMicroUsd` and in the totals, so reconciliation still holds.
 *   * **Negative shared total** (a cloud credit) is allocated by the same rules with the sign
 *     carried through, so a credit lands on the accounts that caused the spend.
 *
 * Throws on inputs that would silently break reconciliation: non-integer or non-finite money, and
 * duplicate account ids (a caller keying rows by account id would lose one).
 */
export function allocateShared(
  accounts: readonly AccountDirectSpend[],
  sharedMicroUsd: number,
  rule: AllocationRule = DEFAULT_ALLOCATION_RULE,
): AllocationResult {
  assertMicroUsd(sharedMicroUsd, "sharedMicroUsd");

  const seen = new Set<string>();
  for (const a of accounts) {
    assertMicroUsd(a.directMicroUsd, `directMicroUsd for account ${a.accountId}`);
    if (seen.has(a.accountId)) {
      throw new Error(`allocateShared: duplicate accountId ${a.accountId}`);
    }
    seen.add(a.accountId);
  }

  const directTotal = accounts.reduce((sum, a) => sum + a.directMicroUsd, 0);

  if (accounts.length === 0) {
    return {
      rule,
      effectiveRule: rule,
      accounts: [],
      sharedMicroUsd,
      allocatedTotalMicroUsd: 0,
      unallocatedMicroUsd: sharedMicroUsd,
      directTotalMicroUsd: 0,
      tenantTotalMicroUsd: sharedMicroUsd,
    };
  }

  // Only positive direct spend earns pro-rata weight; see the negative-credit note above.
  const weights = accounts.map((a) => (a.directMicroUsd > 0 ? BigInt(a.directMicroUsd) : 0n));
  const weightTotal = weights.reduce((sum, w) => sum + w, 0n);

  // Pro rata with no denominator is not an error, it is a tenant whose accounts had no direct spend
  // in the window. Even split is the honest fallback, and the caller is told it happened.
  const effectiveRule: AllocationRule =
    rule === "pro_rata_direct" && weightTotal === 0n ? "even_split" : rule;

  const shares =
    effectiveRule === "pro_rata_direct"
      ? largestRemainder(sharedMicroUsd, weights)
      : largestRemainder(
          sharedMicroUsd,
          accounts.map(() => 1n),
        );

  const allocated = accounts.map((a, i) => ({
    accountId: a.accountId,
    directMicroUsd: a.directMicroUsd,
    allocatedMicroUsd: shares[i],
    totalMicroUsd: a.directMicroUsd + shares[i],
  }));

  return {
    rule,
    effectiveRule,
    accounts: allocated,
    sharedMicroUsd,
    // By construction of largestRemainder this equals sharedMicroUsd. Summed rather than assumed, so
    // the returned object stays self-consistent even if that ever stops being true.
    allocatedTotalMicroUsd: allocated.reduce((sum, a) => sum + a.allocatedMicroUsd, 0),
    unallocatedMicroUsd: 0,
    directTotalMicroUsd: directTotal,
    tenantTotalMicroUsd: directTotal + sharedMicroUsd,
  };
}

/**
 * Split `total` across the given weights so the parts sum exactly to `total`.
 *
 * Largest-remainder: floor each exact share, then hand the leftover units out one at a time to the
 * largest fractional remainders. Sign is carried separately so a negative total (a cloud credit)
 * rounds the same way a positive one does instead of drifting a unit per account.
 */
function largestRemainder(total: number, weights: readonly bigint[]): number[] {
  const shares = weights.map(() => 0);
  const weightTotal = weights.reduce((sum, w) => sum + w, 0n);
  if (total === 0 || weightTotal === 0n) return shares;

  const sign = total < 0 ? -1 : 1;
  const magnitude = BigInt(Math.abs(total));

  const remainders: { index: number; remainder: bigint }[] = [];
  let assigned = 0n;
  weights.forEach((w, index) => {
    const exact = magnitude * w;
    const base = exact / weightTotal; // BigInt division truncates, and both operands are >= 0 here.
    shares[index] = Number(base) * sign;
    assigned += base;
    remainders.push({ index, remainder: exact % weightTotal });
  });

  // Leftover is strictly less than the weight count, so this loop is bounded by the account count.
  let leftover = magnitude - assigned;
  remainders.sort((a, b) => {
    if (a.remainder === b.remainder) return a.index - b.index; // input order breaks ties, so output
    return a.remainder > b.remainder ? -1 : 1; //                  is deterministic across renders
  });
  for (const { index } of remainders) {
    if (leftover <= 0n) break;
    shares[index] += sign;
    leftover -= 1n;
  }

  return shares;
}

function assertMicroUsd(value: number, label: string): void {
  if (!Number.isSafeInteger(value)) {
    // Money is integer micro-USD everywhere in this codebase. Truncating a float here would break
    // the reconciliation guarantee silently, which is worse than failing loudly.
    throw new TypeError(`allocateShared: ${label} must be a safe integer micro-USD, got ${value}`);
  }
}
