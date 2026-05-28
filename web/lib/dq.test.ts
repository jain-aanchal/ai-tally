import { describe, expect, it } from "vitest";
import { classify, dq } from "./dq";

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
    expect(body.ciHalfWidthPct).toBeGreaterThan(mid.ciHalfWidthPct);
  });
});
