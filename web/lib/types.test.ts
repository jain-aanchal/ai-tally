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
});
