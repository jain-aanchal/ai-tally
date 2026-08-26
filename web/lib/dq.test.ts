// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import {
  classify,
  classifyAccountStitching,
  dq,
  withheldByConflicts,
  type AccountStitchConflict,
} from "./dq";

describe("data-quality", () => {
  it("classifies attribution thresholds", () => {
    expect(classify("attribution", 0.95)).toBe("good");
    expect(classify("attribution", 0.8)).toBe("warn");
    expect(classify("attribution", 0.5)).toBe("bad");
  });

  it("zero context drops = good; many = bad", () => {
    expect(classify("drops", 0)).toBe("good");
    expect(classify("drops", 5)).toBe("warn");
    expect(classify("drops", 100)).toBe("bad");
  });

  it("calibration: smaller is better", () => {
    expect(classify("calibration", 0.01)).toBe("good");
    expect(classify("calibration", 0.05)).toBe("warn");
    expect(classify("calibration", 0.1)).toBe("bad");
  });

  it("sampling: tail kept exactly (CI half-width = 0)", () => {
    const tail = dq.sampling.find((s) => s.stratum === "tail")!;
    expect(tail.rate).toBe(1);
    expect(tail.ciHalfWidthPct).toBe(0);
  });

  it("body has wider CI than mid", () => {
    const body = dq.sampling.find((s) => s.stratum === "body")!;
    const mid = dq.sampling.find((s) => s.stratum === "mid")!;
    expect(body.ciHalfWidthPct!).toBeGreaterThan(mid.ciHalfWidthPct!);
  });
});

// CTO-184. One user belongs to one account. The multi-account case must be VISIBLE, so the
// contract these tests pin is: a conflict never attributes, and it never fails to be reported.
describe("account stitching (CTO-184)", () => {
  const conflict = (withheldMicroUsd: number): AccountStitchConflict => ({
    userIdHash: "9f2c41ab7d0e5583",
    accounts: ["3b7e0c19aa41d2f8", "c04d19e6b7a35510"],
    withheldMicroUsd,
    spans30d: 12,
  });

  it("a conflicting user carries at least two candidate accounts", () => {
    // The whole reason we withhold: there is no single right answer to pick.
    expect(conflict(1).accounts.length).toBeGreaterThanOrEqual(2);
  });

  it("no conflicts is good", () => {
    expect(
      classifyAccountStitching({
        directAccounts: 4,
        stitchedAccounts: 9,
        stitchedUsers: 100,
        conflicts: [],
      }),
    ).toBe("good");
  });

  it("a single conflict is bad, not a warning", () => {
    // Not a threshold: it means we are reporting nothing for a real user's real spend.
    expect(
      classifyAccountStitching({
        directAccounts: 4,
        stitchedAccounts: 9,
        stitchedUsers: 100,
        conflicts: [conflict(500_000)],
      }),
    ).toBe("bad");
  });

  it("an absent account dimension is good, not bad", () => {
    // Nothing instrumented yet is a normal state for every existing tenant, not a defect.
    expect(classifyAccountStitching(undefined)).toBe("good");
  });

  it("sums the spend held back across conflicting users", () => {
    expect(withheldByConflicts([conflict(1_500_000), conflict(2_000_000)])).toBe(3_500_000);
    expect(withheldByConflicts([])).toBe(0);
  });

  it("the mock carries a conflict so the surfaced state is exercised", () => {
    const s = dq.accountStitching!;
    expect(s.conflicts).toHaveLength(1);
    expect(s.conflicts[0].accounts).toHaveLength(2);
    // Direct and stitched are counted separately so the UI can show confidence.
    expect(s.directAccounts).toBeGreaterThan(0);
    expect(s.stitchedAccounts).toBeGreaterThan(0);
  });
});
