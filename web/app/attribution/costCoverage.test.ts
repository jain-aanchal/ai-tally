// SPDX-License-Identifier: Apache-2.0
// CTO-429. The three answers the Cost column can give, pinned at the decision layer so the render
// test below it is about the page and not about the arithmetic.

import { describe, expect, it } from "vitest";

import { costCoverage } from "./costCoverage";

const SCOPE = "for this system in this window";

describe("costCoverage", () => {
  it("blanks a system whose spans could none of them be priced", () => {
    // The shape of the bug: sum() skipped every NULL and handed the page a 0.
    const cov = costCoverage(0, 12, 12, SCOPE);
    expect(cov.micro).toBeNull();
    expect(cov.reason).toMatch(/none of the 12 spans .* carry a catalog rate/);
    expect(cov.reason).toMatch(/unknown rather than zero/);
  });

  it("keeps a genuine measured zero, which is a measurement and not a gap", () => {
    const cov = costCoverage(0, 12, 0, SCOPE);
    expect(cov.micro).toBe(0);
    expect(cov.reason).toBe("");
  });

  it("keeps a priced figure", () => {
    const cov = costCoverage(685_000, 40, 0, SCOPE);
    expect(cov.micro).toBe(685_000);
  });

  it("keeps a lower bound that still prints a non-zero figure", () => {
    // Deliberately not a proportional rule (CTO-423): most of the population being unpriced does
    // not blank a figure the reader can still see.
    const cov = costCoverage(685_000, 500, 460, SCOPE);
    expect(cov.micro).toBe(685_000);
    expect(cov.reason).toBe("");
  });

  it("blanks a lower bound that rounds away at display precision", () => {
    const cov = costCoverage(5, 531, 530, SCOPE);
    expect(cov.micro).toBeNull();
    expect(cov.reason).toMatch(/only 1 of 531 spans/);
    expect(cov.reason).toMatch(/rounds to zero/);
  });

  it("does not claim a count it was not given", () => {
    // An older payload carrying no span counts reads as "nobody counted", so the figure stands.
    const cov = costCoverage(685_000, 0, 0, SCOPE);
    expect(cov.micro).toBe(685_000);
  });
});
