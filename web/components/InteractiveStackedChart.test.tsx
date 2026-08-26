// SPDX-License-Identifier: Apache-2.0
// Interaction tests for the interactive stacked chart (CTO-221, D1): the hover tooltip, legend
// toggling, and the onDrill callback. Geometry is covered by rendering, not asserted pixel-wise.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InteractiveStackedChart, type StackedChartDay } from "./InteractiveStackedChart";

const DAYS: StackedChartDay[] = [
  { date: "2026-06-01", byGroup: { openai: 1_000_000, anthropic: 500_000 } },
  { date: "2026-06-02", byGroup: { openai: 2_000_000, anthropic: 250_000 } },
];
const GROUPS = ["openai", "anthropic"];

describe("<InteractiveStackedChart />", () => {
  it("shows a tooltip naming the group, date and value on hover", () => {
    // Legend off so the only "openai" on screen is the tooltip's.
    render(<InteractiveStackedChart days={DAYS} groups={GROUPS} showLegend={false} />);
    // Each day renders one <rect> per group; hover the first day's openai segment.
    const rects = document.querySelectorAll("rect");
    fireEvent.mouseEnter(rects[0]);
    // Tooltip carries the group label, the date and the formatted value.
    expect(screen.getByText("openai")).toBeTruthy();
    expect(screen.getByText(/2026-06-01/)).toBeTruthy();
    expect(screen.getByText(/\$1\.00/)).toBeTruthy();
  });

  it("toggles a series off and back on from the legend", () => {
    const { container } = render(<InteractiveStackedChart days={DAYS} groups={GROUPS} />);
    const rectCount = () => container.querySelectorAll("svg rect").length;
    // 2 days x 2 groups = 4 segments (plus the baseline <line>, not a rect).
    expect(rectCount()).toBe(4);

    // The legend button's accessible name is its text ("anthropic"); it starts pressed (visible).
    const legend = screen.getByRole("button", { name: /anthropic/ });
    expect(legend.getAttribute("aria-pressed")).toBe("true");

    // Hiding anthropic drops its segments and flips aria-pressed.
    fireEvent.click(legend);
    expect(rectCount()).toBe(2);
    expect(legend.getAttribute("aria-pressed")).toBe("false");

    // Toggling back restores them.
    fireEvent.click(legend);
    expect(rectCount()).toBe(4);
    expect(legend.getAttribute("aria-pressed")).toBe("true");
  });

  it("calls onDrill with the clicked group", () => {
    const onDrill = vi.fn();
    render(<InteractiveStackedChart days={DAYS} groups={GROUPS} onDrill={onDrill} />);
    const rects = document.querySelectorAll("svg rect");
    fireEvent.click(rects[0]);
    expect(onDrill).toHaveBeenCalledWith("openai");
  });

  it("renders an empty-state label when there are no days", () => {
    render(<InteractiveStackedChart days={[]} groups={GROUPS} emptyLabel="nothing here" />);
    expect(screen.getByText("nothing here")).toBeTruthy();
  });
});
