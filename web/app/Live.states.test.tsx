// SPDX-License-Identifier: Apache-2.0
// #364, the render half. `three-states.test.ts` pins what each route ANSWERS; this pins what Home
// DRAWS for each of those answers, because the bug was never in the query: the ClickHouse read was
// already returning an honest null, and the page turned it into somebody else's ROI table.
//
// The three assertions that matter are the same on every surface:
//   unavailable -> no figure, and the copy does not claim there is no data.
//   empty       -> no figure, and the copy DOES say nothing has arrived (a real answer).
//   live        -> the tenant's own figures, and no fixture anywhere near them.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HomeLive, type HomePayload } from "./Live";
import type { ForecastPayload } from "@/lib/burndown";
import { LAYERS } from "@/lib/cost";
import { mockRoi } from "@/lib/mock";
import type { SourceState } from "@/lib/dataState";
import type { SpendByLayer, SpendSummary } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(""),
}));

vi.mock("@/lib/useLivePoll", () => ({
  useLivePoll: (_endpoint: string, initial: unknown) => ({ data: initial, updatedAt: new Date() }),
}));

function zeroLayers(): SpendByLayer {
  const out = {} as SpendByLayer;
  for (const l of LAYERS) out[l] = 0;
  return out;
}

// The forecast is read separately from /api/cost/budget and carries its own honest-unavailable
// reason, so it is orthogonal to what is under test here. Pin it to "no section, and here is why".
const FORECAST: ForecastPayload = {
  section: null,
  unavailable: "the telemetry store could not be read",
};

function payload(sources: Partial<Record<keyof HomePayload["sources"], SourceState>>, spend: SpendSummary | null, roi: HomePayload["roi"] = []): HomePayload {
  return {
    spend,
    roi,
    perProviderConversion: [],
    sources: { spend: "live", roi: "live", dq: "live", attribution: "live", ...sources },
  };
}

function renderHome(data: HomePayload) {
  return render(
    <HomeLive initialData={data} enabledLayers={LAYERS} forecast={FORECAST} priorMonthMicroUsd={null} />,
  );
}

describe("Home renders the source state it was given (#364)", () => {
  it("says the source could not be read, and does not claim there is no data", () => {
    renderHome(payload({ spend: "unavailable" }, null));

    expect(screen.getByText(/Source unavailable/i)).toBeTruthy();
    expect(screen.getByText(/This is not a statement that there is no data/i)).toBeTruthy();
    // The page keeps its identity, and prints no money at all.
    expect(screen.getByText("Home")).toBeTruthy();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("says nothing has arrived yet when the read succeeded over zero spans", () => {
    renderHome(payload({ spend: "empty" }, { ...EMPTY_SPEND }));

    expect(screen.getByText(/No AI spend yet/i)).toBeTruthy();
    expect(screen.getByText(/This is what we measured, not a placeholder/i)).toBeTruthy();
    // Not the unavailable copy: an empty read is an answer, not an unknown.
    expect(screen.queryByText(/Source unavailable/i)).toBeNull();
    // And emphatically not a confident $0.00 headline next to a fabricated ROI table.
    expect(screen.queryByText("$0.00")).toBeNull();
    expect(screen.queryByText("research_agent")).toBeNull();
    // Home defers the setup link to SetupCallout, which sits directly above it on the page.
    expect(screen.queryByRole("link", { name: /Finish setup/i })).toBeNull();
  });

  it("draws the tenant's own figures when the rows are real", () => {
    renderHome(
      payload({}, { ...EMPTY_SPEND, totalMicroUsd: 12_340_000, spanCount: 99, reconciledThrough: "1970-01-01" }, [
        { feature: "checkout_helper", costPerUserMicroUsd: 1_000, valuePerUserMicroUsd: null, paybackDays: null, attributionRate: null },
      ]),
    );

    expect(screen.getByText("$12.34")).toBeTruthy();
    expect(screen.getByText("checkout_helper")).toBeTruthy();
    expect(screen.queryByText(/Source unavailable/i)).toBeNull();
    expect(screen.queryByText(/No AI spend yet/i)).toBeNull();
    // No SAMPLE DATA wrapper on a live read: that banner is now reachable only from `sample`.
    expect(screen.queryByText(/SAMPLE DATA/i)).toBeNull();
  });

  it("labels fixtures as sample data on the one path that may still serve them", () => {
    renderHome(
      payload(
        { spend: "sample", roi: "sample" },
        { ...EMPTY_SPEND, totalMicroUsd: 52_400_000_000, spanCount: 1 },
        mockRoi,
      ),
    );

    // The label is necessary and was never sufficient: what makes this safe is that
    // `sampleDataAllowed()` cannot be true while a Clerk organization is resolved.
    expect(screen.getByText(/SAMPLE DATA/i)).toBeTruthy();
    expect(screen.getByText("research_agent")).toBeTruthy();
  });
});

const EMPTY_SPEND: SpendSummary = {
  totalMicroUsd: 0,
  estimatedMicroUsd: 0,
  reconciledMicroUsd: 0,
  reconciledThrough: "1970-01-01",
  byLayer: zeroLayers(),
  unpricedSpanCount: 0,
  spanCount: 0,
};
