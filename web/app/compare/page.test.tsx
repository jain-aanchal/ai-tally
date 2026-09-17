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

// CTO-425. The third fact this page used to collapse into "no telemetry yet": an incumbent with
// real traffic whose cost cannot be known. Telling that workspace its own numbers are synthetic is
// the honesty invariant broken in the unusual direction, so these pin both halves: the sample
// label must not appear, and the pricing gap must be named.
describe("/compare keeps an unpriceable incumbent apart from an empty workspace", () => {
  /** A workspace with real span traffic whose incumbent model carries no catalog rate. */
  const unpricedCurrent = {
    ...comparison,
    current: {
      model: "zephyr-quill-2",
      provider: "acme-labs",
      monthlyCostMicroUsd: null,
      qualityScore: null,
      latencyP95Ms: 1900,
      errorRate: 0.003,
    },
    recommendation: {
      verdict: "mixed" as const,
      summary: "Cannot project savings: some spend over the window could not be priced.",
      projectedSavingsMicroUsd: null,
      projectedSavingsPct: null,
    },
  };

  it("names the pricing gap instead of labelling real traffic synthetic", async () => {
    await renderPage(unpricedCurrent);

    expect(screen.getByText(/No cost rate for/i)).toBeTruthy();
    expect(screen.getAllByText("zephyr-quill-2").length).toBeGreaterThan(0);
    expect(screen.getByText(/some of its spend over the window carries no rate/i)).toBeTruthy();
    // The regression: a customer with 500+ real spans told their own figures were invented, under a
    // CTA for a source they had already connected.
    expect(screen.queryByText(/Sample data/i)).toBeNull();
    expect(screen.queryByText(/These numbers are synthetic/i)).toBeNull();
    expect(screen.queryByText(/Connect a data source/i)).toBeNull();
    // Real traffic, so this is not the new-workspace case either.
    expect(screen.queryByText(NOTHING_ARRIVED)).toBeNull();
  });

  it("still renders the measured body, with the unknown figures blank", async () => {
    await renderPage(unpricedCurrent);

    // The candidate table is the measurement, and it stays on screen rather than being replaced by
    // a preview of somebody else's migration.
    expect(screen.getByText(/current · zephyr-quill-2/i)).toBeTruthy();
    expect(screen.getByText("1900 ms")).toBeTruthy();
  });

  it("does not fire for a priced incumbent: the normal comparison renders in full", async () => {
    await renderPage({
      ...comparison,
      current: { ...comparison.current, model: "zephyr-quill-2", monthlyCostMicroUsd: 8_000_000_000 },
      candidates: [
        {
          model: "zephyr-quill-mini",
          provider: "acme-labs",
          monthlyCostMicroUsd: 2_000_000_000,
          qualityScore: null,
          latencyP95Ms: 1200,
          errorRate: 0.005,
        },
      ],
      recommendation: {
        verdict: "switch",
        summary: "Switch to zephyr-quill-mini.",
        projectedSavingsMicroUsd: 6_000_000_000,
        projectedSavingsPct: 0.75,
      },
    });

    expect(screen.getByText(/Recommendation: switch/i)).toBeTruthy();
    expect(screen.getAllByText("zephyr-quill-mini").length).toBeGreaterThan(0);
    expect(screen.queryByText(/No cost rate for/i)).toBeNull();
    expect(screen.queryByText(/Sample data/i)).toBeNull();
  });

  // A genuine measured zero is a measurement, not an absence (CTO-425). It used to be swept into
  // the empty state by a `=== 0` clause; blanking it as unknown would be the opposite error, so it
  // renders as the zero it was measured to be.
  it("shows a measured zero incumbent as a real figure, not as a sample and not as a blank", async () => {
    await renderPage({
      ...comparison,
      current: { ...comparison.current, model: "zephyr-quill-2", monthlyCostMicroUsd: 0 },
      candidates: [
        {
          model: "zephyr-quill-mini",
          provider: "acme-labs",
          monthlyCostMicroUsd: 0,
          qualityScore: null,
          latencyP95Ms: 1200,
          errorRate: 0.005,
        },
      ],
      recommendation: {
        verdict: "keep",
        summary: "Keep zephyr-quill-2.",
        projectedSavingsMicroUsd: 0,
        projectedSavingsPct: 0,
      },
    });

    expect(screen.queryByText(/Sample data/i)).toBeNull();
    expect(screen.queryByText(/No cost rate for/i)).toBeNull();
    expect(screen.getAllByText("$0.00").length).toBeGreaterThan(0);
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
