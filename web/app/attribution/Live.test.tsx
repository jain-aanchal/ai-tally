// SPDX-License-Identifier: Apache-2.0
// #320 item 3. The rows are `gen_ai.system` values and the column called them "providers", so a
// pinecone row read as a claim that pinecone is an LLM provider. These assertions pin the label to
// what the column actually holds, and pin the vector row to being TAGGED rather than dropped: this
// is a cost-attribution view, and hiding a row would take real spend off a page whose job is to
// account for it.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AttributionLive } from "./Live";
import { buildProviderRow, type AttributionReport } from "@/lib/attribution";

// The page's FilterBar drives the URL, so give it a router the way components/FilterBar.test.tsx does.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/attribution",
  useSearchParams: () => new URLSearchParams(""),
}));

vi.mock("@/lib/useLivePoll", () => ({
  useLivePoll: (_endpoint: string, initial: unknown) => ({ data: initial, updatedAt: new Date() }),
}));

function report(): AttributionReport {
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
  };
}

function renderLive() {
  return render(
    <AttributionLive
      endpoint="/api/attribution"
      initialData={report()}
      outcome="conversion"
      featureTags={[]}
    />,
  );
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
