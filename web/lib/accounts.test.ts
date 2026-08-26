// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  ACCOUNT_HASH_CHARS,
  MIN_SPANS_FOR_COST_PER_USER,
  SHORT_HASH_CHARS,
  type AccountCostRow,
  type AccountCosts,
  accountDisplayName,
  attributedSpend,
  costPerUser,
  emptyAccountRow,
  formatShare,
  isAccountHash,
  layerShare,
  shortenAccountHash,
  trendTotal,
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
