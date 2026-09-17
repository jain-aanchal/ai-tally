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

// CTO-423. The Spend tile carries an "at least" hint when some spans could not be priced, and that
// hint stops meaning anything once the priced remainder rounds away: the reader sees "$0.0000" and
// takes it as a measurement. These pin the three cases apart on the rendered page.
describe("Home does not render a lower bound that rounds to zero (CTO-423)", () => {
  it("blanks the Spend tile when one priced span of 5 micro-USD stands for 531", () => {
    renderHome(
      payload({}, {
        ...EMPTY_SPEND,
        totalMicroUsd: 5,
        byLayer: { ...zeroLayers(), llm: 5 },
        spanCount: 531,
        unpricedSpanCount: 530,
      }),
    );

    expect(screen.queryByText("$0.0000")).toBeNull();
    // The blank is explained, and the explanation says which situation the reader is in.
    const blank = screen.getAllByText(/No value: only 1 of 531 spans/i);
    expect(blank.length).toBeGreaterThan(0);
  });

  it("does not print a share for a value it just blanked (CTO-427)", () => {
    // The production shape: LLM spend prices to a real figure while the non-LLM layers, understated
    // by the same unpriced spans, round away. The Hidden cost tile blanked its value and then said
    // "0% of spend" for that same quantity, which is the tile contradicting itself one line apart.
    renderHome(
      payload({}, {
        ...EMPTY_SPEND,
        totalMicroUsd: 685_000,
        byLayer: { ...zeroLayers(), llm: 685_000 },
        spanCount: 568,
        unpricedSpanCount: 538,
      }),
    );

    // The spend headline is a real figure, so the denominator is reportable.
    expect(screen.getByText("$0.685")).toBeTruthy();
    // The hidden-cost VALUE blanks. Asserted as the ABSENCE of the figure it would otherwise
    // print, because a blank's reason text is shared with the other tiles on this payload: an
    // assertion that merely finds that reason somewhere on the page is satisfied by a sibling
    // and pins nothing. Hidden cost is the only quantity here that would render "$0.00".
    expect(screen.queryByText("$0.00")).toBeNull();
    expect(screen.getAllByText(/No value: only 30 of 568 spans/i).length).toBeGreaterThan(0);
    // And its share is no percentage at all, rather than a 0% that contradicts the blank above it.
    expect(screen.queryByText(/0% of spend/i)).toBeNull();
    expect(screen.getByText(/vector \+ tools \+ compute/i)).toBeTruthy();
  });

  it("blanks Reconciled and its share on the same window, not just Hidden cost (CTO-427)", () => {
    // Reconciled is understated by the same unpriced spans and had no guard at all, so it printed
    // "$0.0000, 0% invoice-confirmed" beside a Spend tile blanking for exactly that reason.
    // reconciledThrough has to be set or the percentage branch never renders and the gap hides.
    renderHome(
      payload({}, {
        ...EMPTY_SPEND,
        totalMicroUsd: 685_000,
        reconciledMicroUsd: 5,
        reconciledThrough: "2026-09-01",
        byLayer: { ...zeroLayers(), llm: 685_000 },
        spanCount: 568,
        unpricedSpanCount: 538,
      }),
    );

    expect(screen.queryByText("$0.0000")).toBeNull();
    expect(screen.queryByText(/invoice-confirmed/i)).not.toBeNull();
    expect(screen.queryByText(/0% invoice-confirmed/i)).toBeNull();
  });

  it("keeps a small lower bound that still renders as a figure", () => {
    renderHome(
      payload({}, {
        ...EMPTY_SPEND,
        totalMicroUsd: 100,
        byLayer: { ...zeroLayers(), llm: 100 },
        spanCount: 531,
        unpricedSpanCount: 530,
      }),
    );

    expect(screen.getByText("$0.0001")).toBeTruthy();
    // And it still says it is a lower bound.
    expect(screen.getByText(/at least: 530 of 531 spans/i)).toBeTruthy();
  });

  it("still shows a genuine measured zero: spans observed, none unpriced", () => {
    renderHome(
      payload({}, { ...EMPTY_SPEND, spanCount: 531, unpricedSpanCount: 0 }),
    );

    // Zero IS the measurement here, and blanking it would be the opposite error.
    expect(screen.getAllByText("$0.00").length).toBeGreaterThan(0);
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
