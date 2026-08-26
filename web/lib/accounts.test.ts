// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  MIN_SPANS_FOR_COST_PER_USER,
  SHORT_HASH_CHARS,
  type AccountCostRow,
  type AccountCosts,
  type ExcludedInfraCost,
  attributedSpend,
  costPerUser,
  emptyAccountRow,
  excludedShare,
  formatShare,
  shortenAccountHash,
  unattributedShare,
} from "./accounts";

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
