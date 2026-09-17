// SPDX-License-Identifier: Apache-2.0
// ExploreChartCard headline figure (CTO-426, following CTO-423). The chart header was the one
// surface the original fix left: the slice total is non-null the moment a single span prices, so a
// population that is 530-of-531 unpriced still rendered "$0.0000" there.

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/cost",
  useSearchParams: () => new URLSearchParams(""),
}));

import { ExploreChartCard } from "./ExploreChartCard";

function respond(totalMicroUsd: number | null, spanCount: number, unpricedSpanCount: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      json: async () => ({
        source: "live",
        groupBy: "model",
        series: {
          groupBy: "model",
          windowStart: "2026-08-19",
          windowEnd: "2026-09-17",
          windowDays: 30,
          groups: [],
          days: [],
          breakdown: [],
          totalMicroUsd,
          truncatedGroups: 0,
          unknownCostGroups: [],
          spanCount,
          unpricedSpanCount,
        },
      }),
    })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("<ExploreChartCard /> headline figure", () => {
  it("blanks a lower bound that rounds away, rather than printing $0.0000", async () => {
    // The production shape: one priced span worth 5 micro-USD standing for 531.
    respond(5, 531, 530);
    render(<ExploreChartCard title="Cost over time" />);

    await waitFor(() => expect(screen.queryByText("$0.0000")).toBeNull());
    // The reason names the partial case, because "all 531 spans" would be a false statement here.
    expect(screen.getByText(/530 of 531 spans in this slice could not be priced/)).toBeTruthy();
  });

  it("keeps a lower bound that still prints a figure, however much is unpriced", async () => {
    // Not a proportional rule: 530 of 531 unpriced, but what priced is large enough to show.
    respond(100_000, 531, 530);
    render(<ExploreChartCard title="Cost over time" />);

    await waitFor(() => expect(screen.getByText("$0.100")).toBeTruthy());
  });

  it("still reports a genuine measured zero, which is a real measurement", async () => {
    // Nothing unpriced, so the total is known and it is zero. Blanking this would be the opposite
    // error: presenting a real measurement as an unknown.
    respond(0, 531, 0);
    render(<ExploreChartCard title="Cost over time" />);

    await waitFor(() => expect(screen.getByText("$0.00")).toBeTruthy());
  });

  it("keeps the all-unpriced wording when nothing in the slice priced", async () => {
    respond(null, 531, 531);
    render(<ExploreChartCard title="Cost over time" />);

    await waitFor(() =>
      expect(screen.getByText(/all 531 spans in this slice could not be priced/)).toBeTruthy(),
    );
  });
});
