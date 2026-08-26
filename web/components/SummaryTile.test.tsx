// SPDX-License-Identifier: Apache-2.0
// SummaryTile honesty (CTO-221, D1): an unknown value is the honest blank with a reason, never a
// fabricated zero, and the delta is colored by whether the move is good given the tile's direction.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SummaryTile } from "./SummaryTile";

describe("<SummaryTile />", () => {
  it("renders a known value through Money", () => {
    render(<SummaryTile label="Spend" micro={52_400_000} />);
    expect(screen.getByText("$52.40")).toBeTruthy();
  });

  it("renders the honest blank with its reason when the value is null", () => {
    render(<SummaryTile label="Spend" micro={null} reason="no cost data for this window" />);
    expect(screen.getByText("No value: no cost data for this window")).toBeTruthy();
  });

  it("tints a cost increase bad and a decrease good by default", () => {
    const { rerender } = render(<SummaryTile label="Spend" micro={100} delta={0.12} />);
    expect(document.querySelector(".text-bad")).toBeTruthy();
    rerender(<SummaryTile label="Spend" micro={100} delta={-0.12} />);
    expect(document.querySelector(".text-good")).toBeTruthy();
  });

  it("flips the delta coloring when higherIsBetter", () => {
    render(<SummaryTile label="Value" micro={100} delta={0.2} higherIsBetter />);
    expect(document.querySelector(".text-good")).toBeTruthy();
  });

  it("shows a blank delta with a reason rather than zero when the change is unknown", () => {
    render(<SummaryTile label="Spend" micro={100} delta={null} deltaReason="no prior period" />);
    expect(screen.getByText("No value: no prior period")).toBeTruthy();
  });
});
