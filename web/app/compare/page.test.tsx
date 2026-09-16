// SPDX-License-Identifier: Apache-2.0
// CTO-395 review, the render half. The route change is only half the fix: what the customer is told
// is decided here, and the two facts the route now distinguishes have to stay distinguished on
// screen. A failed read must render SourceUnavailable, which makes no claim about whether data
// exists; only a successful read that found nothing earns the "nothing has arrived" empty state.
//
// The heavy interactive children are stubbed: they pull the Next navigation hooks and a live poll,
// and none of that is what these cases are about.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ apiGet: vi.fn() }));
vi.mock("@/components/ExploreChartCard", () => ({ ExploreChartCard: () => null }));
vi.mock("@/components/FilterBar", () => ({ FilterBar: () => null }));

import ComparePage from "./page";
import { apiGet } from "@/lib/api";
import {
  CURRENT_MODEL_UNREADABLE_REASON,
  NO_REPLAY_RAN_REASON,
  REPLAY_UNREADABLE_REASON,
  comparison,
  type Comparison,
} from "@/lib/compare";

const mockApiGet = apiGet as unknown as ReturnType<typeof vi.fn>;

async function renderPage(payload: Comparison) {
  mockApiGet.mockResolvedValueOnce(payload);
  render(await ComparePage({}));
}

/** The claim only a successful read may make. */
const NOTHING_ARRIVED = /No model has served traffic for this workspace/i;

describe("/compare keeps a failed read apart from an empty workspace", () => {
  it("renders the source-unavailable shape when the incumbent read failed", async () => {
    await renderPage({
      ...comparison,
      unavailable: CURRENT_MODEL_UNREADABLE_REASON,
      workload: null,
      current: null,
      candidates: [],
      recommendation: null,
    });

    expect(screen.getByText(/Source unavailable/i)).toBeTruthy();
    expect(screen.getByText(/we cannot tell whether any traffic exists/i)).toBeTruthy();
    // The regression: this is what a customer in the provisioning race used to be told.
    expect(screen.queryByText(NOTHING_ARRIVED)).toBeNull();
  });

  it("still tells a genuinely empty workspace that nothing has arrived", async () => {
    await renderPage({
      ...comparison,
      unavailable: null,
      workload: null,
      current: null,
      candidates: [],
      recommendation: null,
    });

    expect(screen.getByText(NOTHING_ARRIVED)).toBeTruthy();
    expect(screen.queryByText(/Source unavailable/i)).toBeNull();
  });
});

describe("the replay diagnostics blank states its true reason", () => {
  it("says the read failed, not that no replay ran, when the replay read threw", async () => {
    await renderPage({
      ...comparison,
      diagnostics: { ...comparison.diagnostics, replayUnavailableReason: REPLAY_UNREADABLE_REASON },
    });

    expect(screen.getAllByText(/could not be read, so we do not know what has been replayed/i).length)
      .toBeGreaterThan(0);
    // The false reason this blank used to carry on a failed read.
    expect(screen.queryByText(/no cross-provider replay has run/i)).toBeNull();
  });

  it("says no replay has run when that is what happened", async () => {
    await renderPage({
      ...comparison,
      diagnostics: { ...comparison.diagnostics, replayUnavailableReason: NO_REPLAY_RAN_REASON },
    });

    expect(screen.getAllByText(/no cross-provider replay has run/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/could not be read, so we do not know what has been replayed/i)).toBeNull();
  });
});
