// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  ACCOUNT_HASH_CHARS,
  ALLOCATION_RULE_DESCRIPTIONS,
  ALLOCATION_RULE_LABELS,
  MAJORITY_UNATTRIBUTED,
  MARGIN_EXCLUDES_INFRA,
  MIN_SPANS_FOR_COST_PER_USER,
  MIN_SPANS_FOR_MARGIN,
  SHORT_HASH_CHARS,
  type AccountCostRow,
  type AccountCosts,
  accountDisplayName,
  accountMargin,
  type ExcludedInfraCost,
  accountsView,
  allocateAccountCosts,
  allocatedRowsTotal,
  attributedSpend,
  costPerUser,
  emptyAccountRow,
  excludedShare,
  formatShare,
  isAccountHash,
  layerShare,
  shortenAccountHash,
  trendTotal,
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

describe("accountMargin", () => {
  it("treats a netted-to-zero revenue as a measurement, not as an unknown", () => {
    const m = accountMargin(row({ directCostMicroUsd: 1_000_000 }), 0);
    expect(m.revenueMicroUsd).toBe(0);
    // A charge fully refunded is real information: this customer cost us money and paid nothing.
    expect(m.marginMicroUsd).toBe(-1_000_000);
    expect(m.reason).toBeNull();
  });

  it("refuses to compute a margin against an assumed zero when revenue is unknown", () => {
    const m = accountMargin(row({ directCostMicroUsd: 1_000_000 }), null);
    expect(m.revenueMicroUsd).toBeNull();
    expect(m.marginMicroUsd).toBeNull();
    expect(m.reason).toMatch(/no revenue source wired/i);
    // No caveats on a blank: there is no number to qualify.
    expect(m.caveats).toEqual([]);
  });

  it("says so when the revenue read failed, rather than blaming the tenant's wiring", () => {
    const m = accountMargin(row(), null, true);
    expect(m.reason).toMatch(/could not be read/i);
    expect(m.reason).toMatch(/not evidence/i);
  });

  it("carries the excluded-infrastructure caveat on every margin it prints", () => {
    const m = accountMargin(row({ spanCount: 5_000, directCostMicroUsd: 4_000_000 }), 10_000_000);
    expect(m.marginMicroUsd).toBe(6_000_000);
    expect(m.caveats).toContain(MARGIN_EXCLUDES_INFRA);
    // Well measured: no extra caveats beyond the one that applies to every account in v1.
    expect(m.caveats).toHaveLength(1);
  });

  it("flags a margin computed against a cost side we barely measured", () => {
    // The live tenant's shape: $20,000 of uploaded revenue against a few cents of attributed spend.
    const m = accountMargin(row({ spanCount: 4, directCostMicroUsd: 130 }), 20_000_000_000);
    expect(m.marginMicroUsd).toBe(20_000_000_000 - 130);
    expect(m.caveats).toContain(MARGIN_EXCLUDES_INFRA);
    expect(m.caveats.some((c) => c.includes(`${MIN_SPANS_FOR_MARGIN}-span floor`))).toBe(true);
    expect(m.caveats.some((c) => /under 1% of revenue/.test(c))).toBe(true);
  });

  it("does not raise the cost-to-revenue flag on a normal account", () => {
    const m = accountMargin(row({ spanCount: 900, directCostMicroUsd: 300_000 }), 1_000_000);
    expect(m.caveats.some((c) => /under 1% of revenue/.test(c))).toBe(false);
  });

  it("does not divide by a zero revenue when checking the cost-to-revenue ratio", () => {
    const m = accountMargin(row({ spanCount: 900, directCostMicroUsd: 300_000 }), 0);
    expect(m.marginMicroUsd).toBe(-300_000);
    expect(m.caveats.some((c) => /under 1% of revenue/.test(c))).toBe(false);
  });
});

// --- Account detail view (CTO-190, plan D4) -----------------------------------------------------

describe("isAccountHash", () => {
  it("accepts a stored 64-character lowercase hex hash", () => {
    expect(isAccountHash("a".repeat(ACCOUNT_HASH_CHARS))).toBe(true);
    expect(isAccountHash("0123456789abcdef".repeat(4))).toBe(true);
  });

  it("rejects anything that could never have been a hash", () => {
    // The shortened display form in particular: it is a truncation for width and is not an id, so
    // a route built from it must not reach a query and must not read as a real account.
    expect(isAccountHash(shortenAccountHash("a".repeat(ACCOUNT_HASH_CHARS)))).toBe(false);
    expect(isAccountHash("")).toBe(false);
    expect(isAccountHash("a".repeat(63))).toBe(false);
    expect(isAccountHash("a".repeat(65))).toBe(false);
    expect(isAccountHash("g".repeat(ACCOUNT_HASH_CHARS))).toBe(false);
    // Uppercase is not what the gateway emits, so accepting it would let one account reach the
    // page under two addresses, only one of which matches a row.
    expect(isAccountHash("A".repeat(ACCOUNT_HASH_CHARS))).toBe(false);
  });

  it("is not fooled by a hash with something appended", () => {
    expect(isAccountHash(`${"a".repeat(ACCOUNT_HASH_CHARS)}/../cost`)).toBe(false);
    expect(isAccountHash(`${"a".repeat(ACCOUNT_HASH_CHARS)}\n`)).toBe(false);
  });
});

describe("accountDisplayName", () => {
  const HASH = "b".repeat(ACCOUNT_HASH_CHARS);

  it("prefers the label and falls back to the shortened hash", () => {
    // The rule the table and the detail header share: clicking "Acme Corp" has to land on a page
    // headed "Acme Corp", and clicking a shortened hash on the same shortened hash.
    expect(accountDisplayName(HASH, "Acme Corp")).toBe("Acme Corp");
    expect(accountDisplayName(HASH, undefined)).toBe(shortenAccountHash(HASH));
  });
});

describe("layerShare", () => {
  it("divides a layer by the account's own direct total", () => {
    const r = row({
      byLayer: { llm: 750_000, tools: 250_000, vector: 0, embeddings: 0 },
      directCostMicroUsd: 1_000_000,
    });
    expect(layerShare(r, "llm")).toBe(0.75);
    expect(layerShare(r, "tools")).toBe(0.25);
    expect(layerShare(r, "vector")).toBe(0);
  });

  it("returns null rather than 0 when the account has no direct spend", () => {
    // "0% of spend is LLM" describes a distribution that does not exist. The page renders a blank.
    expect(layerShare(row({ directCostMicroUsd: 0 }), "llm")).toBeNull();
  });
});

describe("trendTotal", () => {
  it("sums the charted days", () => {
    expect(
      trendTotal([
        { date: "2026-08-01", directCostMicroUsd: 1_000_000 },
        { date: "2026-08-02", directCostMicroUsd: 0 },
        { date: "2026-08-03", directCostMicroUsd: 250_000 },
      ]),
    ).toBe(1_250_000);
  });

  it("is zero for an empty trend", () => {
    expect(trendTotal([])).toBe(0);
  });

  it("catches a day dropped from the chart but still counted in the total", () => {
    // The two-clocks bug: a day list built from the Node clock is shifted against the SQL window,
    // so the oldest day has no slot and falls out of the chart while still counting toward the
    // account's total. The disagreement is invisible on screen unless something states it.
    const full = [
      { date: "2026-07-28", directCostMicroUsd: 500_000 },
      { date: "2026-07-29", directCostMicroUsd: 500_000 },
    ];
    expect(trendTotal(full)).toBe(1_000_000);
    expect(trendTotal(full.slice(1))).not.toBe(trendTotal(full));
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

describe("accountsView", () => {
  // The state machine behind the empty states (CTO-191, plan D5). Each branch produces different
  // copy, and picking the wrong one is how a page tells a tenant with data that it has none, or
  // tells a tenant that already instrumented the SDK to go install it.

  it("is onboarding when not one span in the window carried an account", () => {
    expect(
      accountsView(
        costs({
          unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: 9_000_000 },
          totalDirectMicroUsd: 9_000_000,
        }),
      ),
    ).toBe("onboarding");
  });

  it("is onboarding when the tenant recorded no direct spend at all", () => {
    // Nothing to attribute and nothing to explain away: still a first-run reader, still needs the
    // explainer rather than an empty table.
    expect(accountsView(costs())).toBe("onboarding");
  });

  it("is partial once real accounts exist but most spend still has none", () => {
    expect(
      accountsView(
        costs({
          accounts: [row({ directCostMicroUsd: 1_000_000 })],
          unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: 9_000_000 },
          totalDirectMicroUsd: 10_000_000,
        }),
      ),
    ).toBe("partial");
  });

  it("treats the threshold itself as partial, so a coin-flip split gets the honest copy", () => {
    const half = 5_000_000;
    const view = accountsView(
      costs({
        accounts: [row({ directCostMicroUsd: half })],
        unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: half },
        totalDirectMicroUsd: half * 2,
      }),
    );
    expect(MAJORITY_UNATTRIBUTED).toBe(0.5);
    expect(view).toBe("partial");
  });

  it("is attributed when most spend carries an account", () => {
    expect(
      accountsView(
        costs({
          accounts: [row({ directCostMicroUsd: 9_000_000 })],
          unattributed: { ...emptyAccountRow("", true), directCostMicroUsd: 1_000_000 },
          totalDirectMicroUsd: 10_000_000,
        }),
      ),
    ).toBe("attributed");
  });

  it("does not send a fully instrumented but quiet tenant back to onboarding", () => {
    // Accounts exist with zero spend in the window: a quiet week, not an uninstrumented tenant.
    // Telling this reader to install the SDK they already installed would be plainly wrong.
    expect(
      accountsView(costs({ accounts: [row({ directCostMicroUsd: 0, spanCount: 0 })] })),
    ).toBe("attributed");
  });
});
