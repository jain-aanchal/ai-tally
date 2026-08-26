// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  ALLOCATION_RULES,
  DEFAULT_ALLOCATION_RULE,
  type AccountDirectSpend,
  type AllocationRule,
  allocateShared,
} from "./allocation";

function accts(...direct: number[]): AccountDirectSpend[] {
  return direct.map((d, i) => ({ accountId: `acct-${i}`, directMicroUsd: d }));
}

/**
 * The assertion the whole feature rests on. Run against every case below, not just the tidy ones:
 * a rounding bug shows up on awkward input, never on 100 split three ways.
 */
function expectReconciles(
  accounts: readonly AccountDirectSpend[],
  shared: number,
  rule: AllocationRule,
) {
  const r = allocateShared(accounts, shared, rule);

  const allocatedSum = r.accounts.reduce((s, a) => s + a.allocatedMicroUsd, 0);
  // Allocated shares sum EXACTLY to the shared total, nothing lost, nothing double-counted.
  expect(allocatedSum + r.unallocatedMicroUsd).toBe(shared);
  expect(r.allocatedTotalMicroUsd).toBe(allocatedSum);

  // Direct plus allocated sums exactly to the tenant total.
  const totalSum = r.accounts.reduce((s, a) => s + a.totalMicroUsd, 0);
  expect(totalSum + r.unallocatedMicroUsd).toBe(r.tenantTotalMicroUsd);
  expect(r.tenantTotalMicroUsd).toBe(r.directTotalMicroUsd + shared);

  for (const a of r.accounts) {
    expect(Number.isSafeInteger(a.allocatedMicroUsd)).toBe(true);
    expect(Number.isSafeInteger(a.totalMicroUsd)).toBe(true);
    expect(a.totalMicroUsd).toBe(a.directMicroUsd + a.allocatedMicroUsd);
  }
  return r;
}

describe("pro rata on direct spend", () => {
  it("splits in proportion to direct spend", () => {
    const r = expectReconciles(accts(1_000_000, 3_000_000), 4_000_000, "pro_rata_direct");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([1_000_000, 3_000_000]);
    expect(r.effectiveRule).toBe("pro_rata_direct");
  });

  it("is the default rule", () => {
    expect(DEFAULT_ALLOCATION_RULE).toBe("pro_rata_direct");
    const explicit = allocateShared(accts(1, 2), 90, "pro_rata_direct");
    expect(allocateShared(accts(1, 2), 90)).toEqual(explicit);
  });

  it("gives an account with zero direct spend zero allocated cost", () => {
    const r = expectReconciles(accts(0, 5_000_000), 1_000_000, "pro_rata_direct");
    expect(r.accounts[0].allocatedMicroUsd).toBe(0);
    expect(r.accounts[1].allocatedMicroUsd).toBe(1_000_000);
  });

  it("hands the rounding remainder to the largest remainders, losing nothing", () => {
    // 10 micro-USD over three equal accounts: 3, 3, 3 leaves 1 over.
    const r = expectReconciles(accts(1, 1, 1), 10, "pro_rata_direct");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([4, 3, 3]);
  });

  it("breaks remainder ties by input order, deterministically", () => {
    const a = allocateShared(accts(1, 1, 1, 1), 6, "pro_rata_direct");
    const b = allocateShared(accts(1, 1, 1, 1), 6, "pro_rata_direct");
    expect(a.accounts.map((x) => x.allocatedMicroUsd)).toEqual([2, 2, 1, 1]);
    expect(b.accounts.map((x) => x.allocatedMicroUsd)).toEqual(
      a.accounts.map((x) => x.allocatedMicroUsd),
    );
  });

  it("does not lose a unit on a large bill that divides badly", () => {
    // Roughly the shape of the demo tenant: shared is about 47 percent of the bill.
    const direct = [12_345_679, 7_777_777, 3_333_333, 991, 1];
    expectReconciles(accts(...direct), 20_874_953, "pro_rata_direct");
  });
});

describe("even split", () => {
  it("divides the shared total by account count", () => {
    const r = expectReconciles(accts(1_000_000, 3_000_000), 4_000_000, "even_split");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([2_000_000, 2_000_000]);
  });

  it("ignores direct spend entirely, which is why it flatters heavy accounts", () => {
    const r = allocateShared(accts(0, 99_000_000), 1_000_000, "even_split");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([500_000, 500_000]);
  });

  it("spreads an indivisible total to the first accounts, summing exactly", () => {
    const r = expectReconciles(accts(5, 5, 5), 100, "even_split");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([34, 33, 33]);
  });
});

describe("awkward inputs", () => {
  it("a single account absorbs the whole shared total under either rule", () => {
    for (const rule of ALLOCATION_RULES) {
      const r = expectReconciles(accts(7), 999_999, rule);
      expect(r.accounts[0].allocatedMicroUsd).toBe(999_999);
      expect(r.accounts[0].totalMicroUsd).toBe(1_000_006);
    }
  });

  it("zero accounts reports the shared total as unallocated rather than dropping it", () => {
    for (const rule of ALLOCATION_RULES) {
      const r = expectReconciles([], 4_200_000, rule);
      expect(r.accounts).toEqual([]);
      expect(r.unallocatedMicroUsd).toBe(4_200_000);
      expect(r.tenantTotalMicroUsd).toBe(4_200_000);
    }
  });

  it("falls back to an even split when every account has zero direct spend", () => {
    const r = expectReconciles(accts(0, 0, 0), 10, "pro_rata_direct");
    expect(r.rule).toBe("pro_rata_direct");
    expect(r.effectiveRule).toBe("even_split"); // the page must be able to say the rule changed
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([4, 3, 3]);
  });

  it("allocates zero to everyone when there is no shared cost, without dividing by zero", () => {
    for (const rule of ALLOCATION_RULES) {
      const r = expectReconciles(accts(0, 0), 0, rule);
      expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([0, 0]);
    }
  });

  it("no accounts and no shared cost is still consistent", () => {
    const r = expectReconciles([], 0, "pro_rata_direct");
    expect(r.tenantTotalMicroUsd).toBe(0);
  });

  it("gives a negative direct balance no pro-rata weight but keeps it in the totals", () => {
    const r = expectReconciles(accts(-500, 1_000), 100, "pro_rata_direct");
    expect(r.accounts[0].allocatedMicroUsd).toBe(0);
    expect(r.accounts[1].allocatedMicroUsd).toBe(100);
    expect(r.directTotalMicroUsd).toBe(500);
    expect(r.accounts[0].totalMicroUsd).toBe(-500);
  });

  it("falls back to even split when every account balance is negative", () => {
    const r = expectReconciles(accts(-1, -2), 7, "pro_rata_direct");
    expect(r.effectiveRule).toBe("even_split");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([4, 3]);
  });

  it("carries the sign of a negative shared total (a cloud credit) onto the accounts", () => {
    const r = expectReconciles(accts(1, 1, 1), -10, "pro_rata_direct");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([-4, -3, -3]);
  });

  it("keeps exact shares when weight times total exceeds the float integer range", () => {
    // 4e15 micro-USD is a $4bn account: absurd, but weight x total is then ~4e21, far past 2^53.
    // Float arithmetic would drift here, and drift is exactly what breaks the sum guarantee.
    const big = 4_000_000_000_000_000;
    const r = expectReconciles(accts(big, big, 3), 1_000_003, "pro_rata_direct");
    expect(r.accounts.map((a) => a.allocatedMicroUsd)).toEqual([500_002, 500_001, 0]);
  });
});

describe("inputs that would break reconciliation", () => {
  it("rejects float dollars posing as micro-USD", () => {
    expect(() => allocateShared(accts(1), 10.5)).toThrow(TypeError);
    expect(() => allocateShared([{ accountId: "a", directMicroUsd: 0.1 }], 10)).toThrow(TypeError);
  });

  it("rejects non-finite money", () => {
    expect(() => allocateShared(accts(1), Number.NaN)).toThrow(TypeError);
    expect(() => allocateShared(accts(1), Number.POSITIVE_INFINITY)).toThrow(TypeError);
  });

  it("rejects duplicate account ids, which a caller keying by id would silently lose", () => {
    const dupes = [
      { accountId: "a", directMicroUsd: 1 },
      { accountId: "a", directMicroUsd: 2 },
    ];
    expect(() => allocateShared(dupes, 10)).toThrow(/duplicate accountId/);
  });
});

describe("reconciliation holds across generated inputs", () => {
  // Deterministic pseudo-random sweep: awkward account counts, awkward totals, both rules. Seeded so
  // a failure is reproducible rather than a flaky CI run nobody can chase down.
  let seed = 20260825;
  const next = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };

  it("allocated shares always sum exactly to the shared total", () => {
    for (let trial = 0; trial < 500; trial += 1) {
      const count = Math.floor(next() * 12); // includes 0 and 1
      const accounts = accts(
        ...Array.from({ length: count }, () => Math.floor(next() * 5_000_003)),
      );
      const shared = Math.floor(next() * 9_999_991);
      for (const rule of ALLOCATION_RULES) {
        expectReconciles(accounts, shared, rule);
      }
    }
  });
});
