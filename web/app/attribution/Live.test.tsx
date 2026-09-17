// SPDX-License-Identifier: Apache-2.0
// #320 item 3. The rows are `gen_ai.system` values and the column called them "providers", so a
// pinecone row read as a claim that pinecone is an LLM provider. These assertions pin the label to
// what the column actually holds, and pin the vector row to being TAGGED rather than dropped: this
// is a cost-attribution view, and hiding a row would take real spend off a page whose job is to
// account for it.

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AttributionLive, type AttributionPayload } from "./Live";
import { buildProviderRow } from "@/lib/attribution";

// The page's FilterBar drives the URL, so give it a router the way components/FilterBar.test.tsx does.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/attribution",
  useSearchParams: () => new URLSearchParams(""),
}));

vi.mock("@/lib/useLivePoll", () => ({
  useLivePoll: (_endpoint: string, initial: unknown) => ({ data: initial, updatedAt: new Date() }),
}));

function report(): AttributionPayload {
  const perProvider = [
    buildProviderRow("anthropic", 100, 20, 5_000_000),
    buildProviderRow("pinecone", 100, 20, 400_000),
  ];
  return {
    filters: { tag: null, provider: null, outcome: null },
    perProvider,
    totals: {
      sessions: 200,
      conversions: 40,
      costMicroUsd: 5_400_000,
      costPerConversionMicroUsd: 135_000,
    },
    isMock: false,
    // #364: these rows are real, so the payload says so. The table below only renders at all under
    // `state: "live"` (or the labelled `"sample"`), which is what keeps fixtures off a real tenant.
    state: "live",
  };
}

function renderLive(data: AttributionPayload = report()) {
  return render(
    <AttributionLive
      endpoint="/api/attribution"
      initialData={data}
      outcome="conversion"
      featureTags={[]}
    />,
  );
}

/** The shape a tenant with no joined sessions genuinely gets back. */
function emptyReport(state: AttributionPayload["state"]): AttributionPayload {
  return {
    filters: { tag: null, provider: null, outcome: null },
    perProvider: [],
    totals: { sessions: 0, conversions: 0, costMicroUsd: 0, costPerConversionMicroUsd: null },
    isMock: false,
    state,
  };
}

describe("attribution breakdown dimension", () => {
  it("names the column for what it holds, not 'Provider'", () => {
    renderLive();
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent);
    expect(headers).toContain("System");
    expect(headers).not.toContain("Provider");
    // The FilterBar's "Provider" control is untouched: `?provider=` really does take an LLM
    // provider, and it is narrower than the dimension the table breaks down by.
    expect(screen.getByRole("button", { name: /Provider/ })).toBeTruthy();
  });

  it("keeps the vector row and tags it rather than passing it off as an LLM provider", () => {
    renderLive();
    expect(screen.getByText("pinecone")).toBeTruthy();
    expect(screen.getByText("vector")).toBeTruthy();
    expect(screen.getByText("anthropic")).toBeTruthy();
  });

  it("explains the dimension in the page itself, not only in a code comment", () => {
    renderLive();
    expect(screen.getByText(/is the LLM provider on an\s+LLM span and the vector vendor/)).toBeTruthy();
  });

  it("stops calling a total that includes vector spend 'LLM cost'", () => {
    renderLive();
    expect(screen.getByText("Total cost")).toBeTruthy();
    expect(screen.queryByText("LLM cost")).toBeNull();
  });
});

// #364. The old page had one branch (`isMock ? preview : body`) and so had no way to say "this
// workspace has no sessions yet" other than by drawing the fixture's 5,300 of them behind a label.
describe("attribution source states (#364)", () => {
  it("says the source could not be read, and claims nothing about whether data exists", () => {
    renderLive(emptyReport("unavailable"));
    expect(screen.getByText(/Source unavailable/i)).toBeTruthy();
    expect(screen.getByText(/This is not a statement that there is no data/i)).toBeTruthy();
    expect(screen.queryByText("anthropic")).toBeNull();
  });

  it("says there are no attributed sessions yet when the read succeeded over nothing", () => {
    renderLive(emptyReport("empty"));
    expect(screen.getByText(/No attributed conversion sessions yet/i)).toBeTruthy();
    expect(screen.queryByText(/Source unavailable/i)).toBeNull();
    // Never a 0% conversion rate or a $0.00 per conversion: no session was measured to divide by.
    expect(screen.queryByText("0.0%")).toBeNull();
  });

  it("draws the rows, with no sample banner, when they are real", () => {
    renderLive();
    expect(screen.getByText("anthropic")).toBeTruthy();
    expect(screen.queryByText(/SAMPLE DATA/i)).toBeNull();
    expect(screen.queryByText(/Source unavailable/i)).toBeNull();
    expect(screen.queryByText(/No attributed/i)).toBeNull();
  });

  it("labels the fixture report as sample data on the demo-only path", () => {
    renderLive({ ...report(), isMock: true, state: "sample" });
    expect(screen.getByText(/SAMPLE DATA/i)).toBeTruthy();
  });
});

// CTO-429. The per-system table formatted a NULL-skipping `sum()`: a system whose spans all lack a
// catalog rate summed to 0, not to null, so `<Money>` had nothing to blank on and the page printed
// "$0.00" for a real cost under a footnote asserting every row was real spend. These pin the three
// answers apart, and pin the figures derived from an unknown cost to inheriting the unknown.
describe("unpriced systems (CTO-429)", () => {
  /** One priced system, one all-unpriced system, and one that genuinely spent nothing. */
  function mixedReport(): AttributionPayload {
    const perProvider = [
      // Priced: 60 spans, none unpriced.
      buildProviderRow("anthropic", 40, 8, 685_000, null, 0, 60),
      // Spans observed, all of them unpriced. The sum is 0 because there was nothing to add.
      buildProviderRow("cohere", 12, 4, 0, null, 12, 12),
      // Spans observed, every one priced, and the spend really was nothing. A measurement.
      buildProviderRow("groq", 9, 3, 0, null, 0, 30),
    ];
    return {
      filters: { tag: null, provider: null, outcome: null },
      perProvider,
      totals: {
        sessions: 61,
        conversions: 15,
        costMicroUsd: 685_000,
        // Unpriced spans in the window, so the roll-up ratio is unknown (queryAttribution nulls it).
        costPerConversionMicroUsd: null,
        unpricedSpanCount: 12,
        spanCount: 102,
      },
      isMock: false,
      state: "live",
    };
  }

  function row(system: string): HTMLElement {
    const cell = screen.getByText(system);
    const tr = cell.closest("tr");
    expect(tr).toBeTruthy();
    return tr as HTMLElement;
  }

  it("blanks an all-unpriced system with a reason instead of fabricating $0.00", () => {
    renderLive(mixedReport());
    const cohere = within(row("cohere"));
    expect(cohere.queryByText("$0.00")).toBeNull();
    expect(
      cohere.getByText(/none of the 12 spans for this system in this window carry a catalog rate/),
    ).toBeTruthy();
  });

  it("still shows a fully priced system's figure", () => {
    renderLive(mixedReport());
    expect(within(row("anthropic")).getByText("$0.685")).toBeTruthy();
  });

  it("still shows a genuine measured zero, which is a measurement and not a gap", () => {
    renderLive(mixedReport());
    const groq = within(row("groq"));
    expect(groq.getAllByText("$0.00").length).toBeGreaterThan(0);
    expect(groq.queryByText(/carry a catalog rate/)).toBeNull();
  });

  it("makes every figure derived from an unknown cost unknown too", () => {
    renderLive(mixedReport());
    const cohere = within(row("cohere"));
    // $/conversion, value/user and margin/user all blank, and all three send the reader to the
    // cost rather than to a conversion count or a revenue connector that is not the problem.
    const blamed = cohere.getAllByText(/the cost for this system is unknown, not zero/);
    expect(blamed.length).toBe(3);
    // The row has four conversions, so "nothing to divide by" would be a wrong explanation.
    expect(cohere.queryByText(/no conversion events for this system/)).toBeNull();
  });

  it("blanks the headline tiles when nothing in the window could be priced", () => {
    const data = mixedReport();
    data.perProvider = [buildProviderRow("cohere", 12, 4, 0, null, 12, 12)];
    data.totals = {
      sessions: 12,
      conversions: 4,
      costMicroUsd: 0,
      costPerConversionMicroUsd: null,
      unpricedSpanCount: 12,
      spanCount: 12,
    };
    renderLive(data);
    const tile = screen.getByText("Total cost").closest("div") as HTMLElement;
    expect(within(tile).queryByText("$0.00")).toBeNull();
    expect(
      within(tile).getByText(/none of the 12 spans in this window carry a catalog rate/),
    ).toBeTruthy();
    expect(
      screen.getByText(/the total cost for this window is unknown, not zero/),
    ).toBeTruthy();
  });

  it("keeps the headline tiles when the window is fully priced", () => {
    const data = mixedReport();
    data.perProvider = [buildProviderRow("anthropic", 40, 8, 685_000, null, 0, 60)];
    data.totals = {
      sessions: 40,
      conversions: 8,
      costMicroUsd: 685_000,
      costPerConversionMicroUsd: 85_625,
      unpricedSpanCount: 0,
      spanCount: 60,
    };
    renderLive(data);
    const tile = screen.getByText("Total cost").closest("div") as HTMLElement;
    expect(within(tile).getByText("$0.685")).toBeTruthy();
  });

  it("stops claiming in the footnote that every row is real spend", () => {
    renderLive(mixedReport());
    expect(screen.queryByText(/Every row is real spend for this window/)).toBeNull();
    expect(screen.getByText(/never a zero/)).toBeTruthy();
  });
});
