// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  ALLOCATION_RULE_DESCRIPTIONS,
  ALLOCATION_RULE_LABELS,
  MIN_SPANS_FOR_COST_PER_USER,
  SHORT_HASH_CHARS,
  type AccountCostRow,
  type AccountCosts,
  type ExcludedInfraCost,
  allocateAccountCosts,
  allocatedRowsTotal,
  attributedSpend,
  costPerUser,
  emptyAccountRow,
  excludedShare,
  formatShare,
  shortenAccountHash,
  unattributedShare,
} from "./accounts";
import { ALLOCATION_RULES, type AllocationRule } from "./allocation";

function row(over: Partial<AccountCostRow> = {}): AccountCostRow {
  return {
    ...emptyAccountRow("a".repeat(64), false),
    directCostMicroUsd: 1_000_000,
    distinctUsers: 4,
    spanCount: 500,
    ...over,
  };
}

function costs(over: Partial<AccountCosts> = {}): AccountCosts {
  return {
    windowDays: 30,
    accounts: [],
    unattributed: emptyAccountRow("", true),
    totalDirectMicroUsd: 0,
    ...over,
  };
}

describe("costPerUser", () => {
  it("divides direct cost by distinct users above the sample floor", () => {
    expect(costPerUser(row({ directCostMicroUsd: 1_000_000, distinctUsers: 4 }))).toEqual({
      micro: 250_000,
      reason: null,
    });
  });

  it("suppresses the ratio below the sample floor, naming the shortfall", () => {
    const result = costPerUser(row({ spanCount: MIN_SPANS_FOR_COST_PER_USER - 1 }));
    expect(result.micro).toBeNull();
    expect(result.reason).toContain(String(MIN_SPANS_FOR_COST_PER_USER));
  });

  it("prints the ratio exactly at the floor", () => {
    expect(costPerUser(row({ spanCount: MIN_SPANS_FOR_COST_PER_USER })).micro).not.toBeNull();
  });

  it("blanks with a different reason when there are no users to divide by", () => {
    // Distinct from the low-sample blank on purpose: a busy account with no user ids is a
    // different fact from a quiet one, and dividing by zero would print Infinity.
    const result = costPerUser(row({ distinctUsers: 0, spanCount: 10_000 }));
    expect(result.micro).toBeNull();
    expect(result.reason).toContain("nothing to divide by");
  });
});

describe("shortenAccountHash", () => {
  it("truncates a full hash and marks the truncation", () => {
    const hash = "cf0f8e1442e65b0367807e613417a147067c30117dfe7ca0bc6b803f441b5765";
    const short = shortenAccountHash(hash);
    expect(short.startsWith(hash.slice(0, SHORT_HASH_CHARS))).toBe(true);
    expect(short.endsWith("…")).toBe(true);
  });

  it("leaves an already-short value alone rather than adding a misleading ellipsis", () => {
    expect(shortenAccountHash("abc")).toBe("abc");
  });
});

describe("unattributedShare", () => {
  it("reports the share of direct spend with no account", () => {
    const c = costs({
      unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: 750_000 },
      totalDirectMicroUsd: 1_000_000,
    });
    expect(unattributedShare(c)).toBeCloseTo(0.75);
  });

  it("reports 100 percent when nothing is tagged, which is the local-tenant state", () => {
    const c = costs({
      unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: 500_000 },
      totalDirectMicroUsd: 500_000,
    });
    expect(unattributedShare(c)).toBe(1);
  });

  it("is null with no direct spend at all, never a flattering zero", () => {
    expect(unattributedShare(costs())).toBeNull();
  });
});

describe("formatShare", () => {
  it("prints an ordinary share to one decimal", () => {
    expect(formatShare(0.6234)).toBe("62.3%");
  });

  it("refuses to round a near-total share up to a flat 100%", () => {
    // The live local tenant sits here: three real accounts against a huge unattributed bucket.
    // "100.0%" beside a table of three customers contradicts the table.
    expect(formatShare(0.99999)).toBe(">99.9%");
  });

  it("refuses to round a sliver down to a flat 0%", () => {
    expect(formatShare(0.0000004)).toBe("<0.1%");
  });

  it("prints the round numbers only when they are exact", () => {
    expect(formatShare(1)).toBe("100.0%");
    expect(formatShare(0)).toBe("0.0%");
  });
});

describe("attributedSpend", () => {
  it("is the total less the unattributed bucket, so it reconciles with the table", () => {
    const accounts = [row({ directCostMicroUsd: 300_000 }), row({ directCostMicroUsd: 200_000 })];
    const c = costs({
      accounts,
      unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: 500_000 },
      totalDirectMicroUsd: 1_000_000,
    });
    expect(attributedSpend(c)).toBe(500_000);
    expect(attributedSpend(c)).toBe(accounts.reduce((s, a) => s + a.directCostMicroUsd, 0));
  });
});

describe("excludedShare (CTO-189)", () => {
  function excluded(over: Partial<ExcludedInfraCost> = {}): ExcludedInfraCost {
    const compute = over.computeMicroUsd ?? 0;
    const egress = over.egressMicroUsd ?? 0;
    return {
      windowDays: 30,
      computeMicroUsd: compute,
      egressMicroUsd: egress,
      totalMicroUsd: compute + egress,
      ...over,
    };
  }

  it("is excluded over ALL spend, so it reads as a share of the whole bill", () => {
    // The plan's worked example: direct 53, excluded 47, banner says 47 percent.
    const share = excludedShare(
      excluded({ computeMicroUsd: 40_000_000, egressMicroUsd: 7_000_000 }),
      costs({ totalDirectMicroUsd: 53_000_000 }),
    );
    expect(share).toBeCloseTo(0.47);
  });

  it("never exceeds 100 percent when infrastructure outweighs the model bill", () => {
    // Dividing by direct spend alone would print 400%. The denominator is the total for a reason.
    const share = excludedShare(
      excluded({ computeMicroUsd: 4_000_000 }),
      costs({ totalDirectMicroUsd: 1_000_000 }),
    );
    expect(share).toBeCloseTo(0.8);
    expect(share!).toBeLessThanOrEqual(1);
  });

  it("is the whole bill when a tenant's only spend is compute and egress", () => {
    const share = excludedShare(
      excluded({ computeMicroUsd: 2_000_000 }),
      costs({ totalDirectMicroUsd: 0 }),
    );
    expect(share).toBe(1);
  });

  it("is null when the window holds no spend at all, never a flattering zero", () => {
    expect(excludedShare(excluded(), costs({ totalDirectMicroUsd: 0 }))).toBeNull();
  });

  it("hands formatShare a value it will not round to a flat 100%", () => {
    // A sliver of direct spend against a mountain of compute. "100.0%" would say the table below
    // covers nothing, while it plainly covers something. Same trap D2 hit on the unattributed share.
    const share = excludedShare(
      excluded({ computeMicroUsd: 100_000_000 }),
      costs({ totalDirectMicroUsd: 100 }),
    );
    expect(formatShare(share!)).toBe(">99.9%");
  });
});

describe("allocateAccountCosts (CTO-193)", () => {
  function shared(total: number): ExcludedInfraCost {
    return {
      windowDays: 30,
      computeMicroUsd: total,
      egressMicroUsd: 0,
      totalMicroUsd: total,
    };
  }

  /** Three tagged accounts plus a bucket, with the totals wired up the way the query returns them. */
  function tenant(directs: number[], bucketDirect: number): AccountCosts {
    const accounts = directs.map((d, i) => ({
      ...emptyAccountRow(String(i).repeat(64), false),
      directCostMicroUsd: d,
    }));
    const unattributed = {
      ...emptyAccountRow("", true),
      directCostMicroUsd: bucketDirect,
    };
    return {
      windowDays: 30,
      accounts,
      unattributed,
      totalDirectMicroUsd: directs.reduce((s, d) => s + d, 0) + bucketDirect,
    };
  }

  // --- THE ACCEPTANCE TEST ----------------------------------------------------------------------

  /**
   * Direct plus allocated across every row equals the tenant total, exactly.
   *
   * This is the claim the page prints and the one the whole feature rests on: if these rows do not
   * add up to what /cost reports, the tab contradicts the Cost tab and loses trust the first time
   * someone sums a column. C1 guarantees the arithmetic; this asserts the WIRING does not break it,
   * which is the only way it can break.
   */
  function expectReconciles(costs: AccountCosts, excludedTotal: number, rule: AllocationRule) {
    const out = allocateAccountCosts(costs, shared(excludedTotal), rule)!;
    expect(out).not.toBeNull();
    expect(allocatedRowsTotal(out)).toBe(out.tenantTotalMicroUsd);
    expect(out.tenantTotalMicroUsd).toBe(costs.totalDirectMicroUsd + excludedTotal);
    expect(out.allocatedTotalMicroUsd).toBe(excludedTotal);
    expect(out.directTotalMicroUsd).toBe(costs.totalDirectMicroUsd);
    for (const r of [...out.accounts, out.unattributed]) {
      expect(r.totalMicroUsd).toBe(r.directCostMicroUsd + r.allocatedMicroUsd);
    }
    return out;
  }

  it("reconciles on the live tenant's shape: three tiny accounts, a huge untagged bucket", () => {
    // The real numbers, in micro-USD: direct $48,716.15 of which the bucket is all but $5.31, and
    // $42,062.91 of compute and egress to allocate.
    const costs = tenant([3_120_000, 1_450_000, 740_000], 48_710_840_000);
    const out = expectReconciles(costs, 42_062_910_000, "pro_rata_direct");
    expect(out.tenantTotalMicroUsd).toBe(90_779_060_000);
    // Not an absurd per-account figure: each tagged account carries a share proportional to a
    // handful of dollars of direct spend, not a third of a $42k infrastructure bill.
    for (const a of out.accounts) {
      expect(a.allocatedMicroUsd).toBeLessThan(5_000_000);
    }
    // The bucket carries essentially the whole infrastructure bill, because it caused it.
    expect(out.unattributed.allocatedMicroUsd / out.allocatedTotalMicroUsd).toBeGreaterThan(0.999);
  });

  it("reconciles under an even split too, bucket included", () => {
    const costs = tenant([3_120_000, 1_450_000, 740_000], 48_710_840_000);
    const out = expectReconciles(costs, 42_062_910_000, "even_split");
    // Four participants, so the bucket takes a quarter rather than its usage share. That is the
    // rule doing what it says; the point of the assertion is that the bucket still participates.
    expect(out.unattributed.allocatedMicroUsd).toBe(10_515_727_500);
  });

  it("reconciles on a total that does not divide evenly", () => {
    // Rounding across many small accounts is one of the two named ways to break reconciliation.
    expectReconciles(tenant([1, 1, 1], 1), 10, "pro_rata_direct");
    expectReconciles(tenant([7, 11, 13], 17), 1_000_003, "pro_rata_direct");
  });

  // --- the unattributed-bucket decision ---------------------------------------------------------

  it("gives the untagged bucket the infrastructure it caused, not the tagged accounts", () => {
    const costs = tenant([1_000_000], 99_000_000);
    const out = allocateAccountCosts(costs, shared(100_000_000), "pro_rata_direct")!;
    expect(out.accounts[0].allocatedMicroUsd).toBe(1_000_000);
    expect(out.unattributed.allocatedMicroUsd).toBe(99_000_000);
  });

  it("does not let a newly tagged account change what the others cost", () => {
    // The property that rules out excluding the bucket. If the bucket sat out, tagging one more
    // account would redistribute the entire infrastructure bill and halve the existing accounts'
    // cost, so a customer's cost would depend on how many OTHER customers were instrumented.
    const before = allocateAccountCosts(
      tenant([1_000_000], 9_000_000),
      shared(10_000_000),
      "pro_rata_direct",
    )!;
    const after = allocateAccountCosts(
      tenant([1_000_000, 2_000_000], 7_000_000),
      shared(10_000_000),
      "pro_rata_direct",
    )!;
    const firstBefore = before.accounts[0].allocatedMicroUsd;
    const firstAfter = after.accounts.find((a) => a.directCostMicroUsd === 1_000_000)!;
    expect(firstAfter.allocatedMicroUsd).toBe(firstBefore);
  });

  it("keeps the bucket flagged as unattributed so nothing can rank it as a customer", () => {
    const out = allocateAccountCosts(tenant([1_000_000], 1_000_000), shared(10), "even_split")!;
    expect(out.unattributed.unattributed).toBe(true);
    expect(out.accounts.every((a) => !a.unattributed)).toBe(true);
    expect(out.accounts.map((a) => a.accountIdHash)).not.toContain("");
  });

  // --- presentation contracts -------------------------------------------------------------------

  it("ranks accounts by TOTAL cost, not direct", () => {
    // Under an even split the cheaper account by direct spend can be the more expensive customer
    // once infrastructure is counted. A table headed by a Total column must be sorted by it.
    const costs = tenant([3_000_000, 2_000_000], 0);
    costs.accounts[1].spanCount = 10; // irrelevant to cost, present to keep the rows distinct
    const out = allocateAccountCosts(costs, shared(0), "even_split")!;
    expect(out.accounts[0].totalMicroUsd).toBeGreaterThanOrEqual(out.accounts[1].totalMicroUsd);
  });

  it("reports the fallback rule when pro rata has no denominator", () => {
    // Every participant at zero direct spend. Reporting "pro rata" here would name an arithmetic
    // that never ran, so the page names the rule that actually applied.
    const out = allocateAccountCosts(tenant([0, 0], 0), shared(1_000_000), "pro_rata_direct")!;
    expect(out.rule).toBe("pro_rata_direct");
    expect(out.effectiveRule).toBe("even_split");
    expect(allocatedRowsTotal(out)).toBe(out.tenantTotalMicroUsd);
  });

  it("returns null when the shared total could not be read, never a silent zero", () => {
    // A zero allocation would print direct cost as though it were total cost on every row, which
    // is the understatement CTO-189's banner existed to flag, with no banner left to flag it.
    expect(allocateAccountCosts(tenant([1_000_000], 0), null, "pro_rata_direct")).toBeNull();
  });

  it("allocates nothing when the window holds no infrastructure spend", () => {
    const out = allocateAccountCosts(tenant([1_000_000], 0), shared(0), "pro_rata_direct")!;
    expect(out.allocatedTotalMicroUsd).toBe(0);
    expect(out.tenantTotalMicroUsd).toBe(1_000_000);
    expect(allocatedRowsTotal(out)).toBe(1_000_000);
  });

  it("names every rule it can apply, so no column header can be blank", () => {
    for (const rule of ALLOCATION_RULES) {
      expect(ALLOCATION_RULE_LABELS[rule]).toBeTruthy();
      expect(ALLOCATION_RULE_DESCRIPTIONS[rule]).toBeTruthy();
    }
  });
});
