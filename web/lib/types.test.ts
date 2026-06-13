// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import { formatUSD } from "./types";

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
