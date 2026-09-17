// SPDX-License-Identifier: Apache-2.0
// Recoverable Cost tile blanks (CTO-430). A blank has to carry the reason that is actually true:
// "no finding in this category" and "findings exist but none could be priced" are different
// answers, and the tile used to explain both with the second one.

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/waste",
  useSearchParams: () => new URLSearchParams(""),
}));

import { WasteLive } from "./Live";
import type { WasteReport } from "@/lib/waste";

const EMPTY: WasteReport = {
  findings: [],
  totalRecoverableMicroUsd: null,
  byCategory: {
    paid_for_nothing: null,
    duplicated_work: null,
    wrong_sized_model: null,
    no_measured_return: null,
    structural_inefficiency: null,
  },
  generatedForWindowDays: 30,
  unavailable: null,
  hasTelemetry: true,
};

describe("<WasteLive /> tile blanks", () => {
  /**
   * The Recoverable TOTAL tile. Anchored on its hint, which no other element carries: the table
   * below has a "Recoverable" column header with the same text as the tile's label.
   */
  function totalTile(): HTMLElement {
    const hint = screen.getByText(/^last \d+ days · \d+ finding/);
    const tile = hint.closest("div.bg-panel");
    expect(tile).toBeTruthy();
    return tile as HTMLElement;
  }

  it("explains a category with no findings as an absence, not as a failure to price", () => {
    render(<WasteLive initialData={EMPTY} />);

    // The reason that does NOT apply: nothing failed to be bounded, there was nothing to bound.
    // Matched against the string the tile really renders. An earlier spelling of this looked for
    // "could NOT be bounded", which appears in neither reason, so it passed in every state and
    // pinned nothing.
    expect(screen.queryByText(/could be bounded to a dollar amount/i)).toBeNull();
    // The reason that does.
    expect(
      screen.getAllByText(/no finding in this category, so there is no amount to recover/i).length,
    ).toBeGreaterThan(0);
  });

  it("explains the Recoverable total as an absence too when there is no finding at all", () => {
    render(<WasteLive initialData={EMPTY} />);

    // The total tile had the same defect as the category tiles and was fixed in the same commit,
    // but only the category half was pinned: reverting this branch passed the whole suite.
    const tile = totalTile();
    expect(within(tile).getByText(/no finding in this window, so there is no amount to recover/i)).toBeTruthy();
    expect(within(tile).queryByText(/could be bounded to a dollar amount/i)).toBeNull();
  });

  it("keeps the bounding reason when findings exist but none carry an amount", () => {
    const withUnboundedFinding: WasteReport = {
      ...EMPTY,
      findings: [
        {
          category: "structural_inefficiency",
          scopeKind: "feature",
          scopeValue: "checkout",
          recoverableMicroUsd: null,
          windowSpendMicroUsd: 1_000_000,
          confidence: "medium",
          title: "Context bloat on checkout",
          reason: "a finding with no bounded amount",
          evidence: { signal: "context-bloat" },
          drillHref: "/agents",
        },
      ],
    } as WasteReport;

    render(<WasteLive initialData={withUnboundedFinding} />);

    // That category has a finding, so the bounding reason is the true one for it.
    expect(
      screen.getAllByText(/no finding in this category could be bounded to a dollar amount/i)
        .length,
    ).toBeGreaterThan(0);
    // And the total, which has one finding in the window and no bounded dollars anywhere, keeps
    // the bounding reason rather than claiming the window was empty.
    const tile = totalTile();
    expect(within(tile).getByText(/no finding could be bounded to a dollar amount/i)).toBeTruthy();
    expect(within(tile).queryByText(/no finding in this window/i)).toBeNull();
  });
});
