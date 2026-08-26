// SPDX-License-Identifier: Apache-2.0
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BLANK, Blank, Money, Pct } from "./HonestValue";

describe("Blank (CTO-178)", () => {
  it("renders the glyph with the reason on hover", () => {
    const { container } = render(<Blank reason="no revenue events wired" />);
    expect(container.textContent).toContain(BLANK);
    expect(container.querySelector("[title='no revenue events wired']")).toBeTruthy();
  });

  it("also exposes the reason to assistive tech, not just the mouse", () => {
    render(<Blank reason="below 50 samples" />);
    expect(screen.getByText(/No value: below 50 samples/i)).toBeTruthy();
  });

  it("carries a visual affordance so the hover is discoverable", () => {
    const { container } = render(<Blank reason="no eval pass has run" />);
    const cls = container.firstElementChild?.className ?? "";
    expect(cls).toContain("cursor-help");
    expect(cls).toContain("decoration-dotted");
  });
});

describe("Money (CTO-178)", () => {
  it("formats a real amount through formatUSD", () => {
    const { container } = render(<Money micro={12_500_000} />);
    expect(container.textContent).toBe("$12.50");
  });

  it("keeps sub-cent precision rather than flooring it to $0.00", () => {
    const { container } = render(<Money micro={3_200} />);
    expect(container.textContent).toBe("$0.0032");
  });

  it("renders $0.00 for a genuine zero, which is a known value and not a blank", () => {
    const { container } = render(<Money micro={0} />);
    expect(container.textContent).toBe("$0.00");
  });

  it("renders an explained blank for null", () => {
    const { container } = render(<Money micro={null} reason="no revenue events wired" />);
    expect(container.textContent).toContain(BLANK);
    expect(screen.getByText(/No value: no revenue events wired/i)).toBeTruthy();
  });

  it("falls back to a generic reason rather than an unexplained blank", () => {
    const nothing: number | null = null;
    render(<Money micro={nothing} reason={undefined as unknown as string} />);
    expect(screen.getByText(/No value: no cost data for this period/i)).toBeTruthy();
  });
});

// Compile-time half of the contract: `npm run typecheck` fails if omitting `reason` on a nullable
// value ever stops being an error, which is the guarantee that keeps blanks explained.
describe("nullable values must carry a reason", () => {
  it("is enforced by the type checker", () => {
    const unknown: number | null = null;
    expect(() => (
      <>
        {/* @ts-expect-error a nullable amount must carry a reason */}
        <Money micro={unknown} />
        {/* @ts-expect-error a nullable percentage must carry a reason */}
        <Pct value={unknown} />
      </>
    )).toBeTruthy();
  });
});

describe("Pct (CTO-178)", () => {
  it("renders a fraction as a percentage, one decimal by default", () => {
    const { container } = render(<Pct value={0.4217} />);
    expect(container.textContent).toBe("42.2%");
  });

  it("honours the digits prop, since surfaces disagree on precision", () => {
    const { container } = render(<Pct value={0.4217} digits={0} />);
    expect(container.textContent).toBe("42%");
  });

  it("renders 0% for a genuine zero rate", () => {
    const { container } = render(<Pct value={0} digits={0} />);
    expect(container.textContent).toBe("0%");
  });

  it("drops the trailing sign when unit is false, for the low end of a range", () => {
    const { container } = render(<Pct value={0.099} unit={false} />);
    expect(container.textContent).toBe("9.9");
  });

  it("keeps the same rounding whether or not the sign is printed", () => {
    const bare = render(<Pct value={0.12345} unit={false} />).container.textContent;
    const signed = render(<Pct value={0.12345} />).container.textContent;
    expect(`${bare}%`).toBe(signed);
  });

  it("renders an explained blank for null", () => {
    const { container } = render(<Pct value={null} reason="needs ≥50 spans in 7d" />);
    expect(container.textContent).toContain(BLANK);
    expect(screen.getByText(/No value: needs ≥50 spans in 7d/i)).toBeTruthy();
  });

  it("falls back to a generic reason rather than an unexplained blank", () => {
    const nothing: number | null = null;
    render(<Pct value={nothing} reason={undefined as unknown as string} />);
    expect(screen.getByText(/No value: not enough samples to report a rate/i)).toBeTruthy();
  });
});
