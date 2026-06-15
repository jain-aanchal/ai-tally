// SPDX-License-Identifier: Apache-2.0
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PartialDataBanner } from "./DataStateBanner";

describe("PartialDataBanner (CTO-107)", () => {
  it("renders the actionable layer-specific text when given trippedLayers", () => {
    render(<PartialDataBanner trippedLayers={["vector"]} />);
    expect(screen.getByText(/Partial data/i)).toBeTruthy();
    expect(
      screen.getByText(/vector is reporting zero — that connector isn’t producing data/i),
    ).toBeTruthy();
  });

  it("pluralises and joins when multiple layers are tripped", () => {
    render(<PartialDataBanner trippedLayers={["vector", "tools"]} />);
    expect(
      screen.getByText(/vector, tools are reporting zero/i),
    ).toBeTruthy();
  });

  it("falls back to the legacy `missing` copy when trippedLayers is empty", () => {
    render(<PartialDataBanner trippedLayers={[]} missing="a value-event source" />);
    expect(screen.getByText(/a value-event source isn’t connected yet/i)).toBeTruthy();
  });

  it("supports the legacy `missing` prop unchanged for non-layer surfaces", () => {
    render(<PartialDataBanner missing="the replay sampler" />);
    expect(screen.getByText(/the replay sampler isn’t connected yet/i)).toBeTruthy();
  });
});
