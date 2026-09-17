// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { formatUSD, isZeroRoundingLowerBound, roundsToZeroUSD } from "./types";

describe("formatUSD", () => {
  it("formats micro-USD as dollars", () => {
    expect(formatUSD(2_250_000)).toBe("$2.25");
    expect(formatUSD(1_000_000)).toBe("$1.00");
  });

  it("formats large values with separators", () => {
    expect(formatUSD(14_820_000_000)).toBe("$14,820.00");
  });

  it("uses 4 decimals for sub-cent values so AI per-call costs don't floor to zero", () => {
    expect(formatUSD(3_200)).toBe("$0.0032");
    expect(formatUSD(100)).toBe("$0.0001");
  });

  it("uses 3 decimals between one cent and one dollar", () => {
    expect(formatUSD(125_000)).toBe("$0.125");
  });

  it("keeps zero at 2 decimals", () => {
    expect(formatUSD(0)).toBe("$0.00");
  });
});

// CTO-423. A lower bound that disappears at display precision is read as a measured zero, so the
// decision to blank it has to be taken at the precision the formatter actually prints.
describe("roundsToZeroUSD", () => {
  it("is true for a figure the formatter prints without a single non-zero digit", () => {
    expect(formatUSD(5)).toBe("$0.0000");
    expect(roundsToZeroUSD(5)).toBe(true);
    expect(roundsToZeroUSD(0)).toBe(true);
  });

  it("is false as soon as the formatter prints a figure", () => {
    expect(roundsToZeroUSD(100)).toBe(false); // "$0.0001"
    expect(roundsToZeroUSD(3_200)).toBe(false); // "$0.0032"
  });

  it("tracks the formatter rather than a threshold of its own", () => {
    // Every value the formatter renders as all zeros must be reported as rounding to zero, and
    // nothing else must be, whatever precision formatUSD picks for the magnitude.
    for (const micro of [0, 1, 5, 49, 99, 100, 101, 5_000, 999_999, 1_000_000]) {
      expect(roundsToZeroUSD(micro)).toBe(!/[1-9]/.test(formatUSD(micro)));
    }
  });
});

describe("isZeroRoundingLowerBound", () => {
  it("flags the priced remainder of a mostly unpriced population", () => {
    // 530 unpriced spans and one priced span worth 5 micro-USD: "$0.0000 at least".
    expect(isZeroRoundingLowerBound(5, 530)).toBe(true);
  });

  it("leaves a genuine measured zero alone: nothing was unpriced, so zero is the measurement", () => {
    expect(isZeroRoundingLowerBound(0, 0)).toBe(false);
  });

  it("leaves a lower bound that still prints a figure alone, however much is unpriced", () => {
    // No proportional rule here on purpose: 9,999 unpriced spans do not blank a figure that shows.
    expect(isZeroRoundingLowerBound(100, 9_999)).toBe(false);
  });

  it("is false for a value we never had", () => {
    expect(isZeroRoundingLowerBound(null, 530)).toBe(false);
  });
});
