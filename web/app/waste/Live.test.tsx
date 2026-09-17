// SPDX-License-Identifier: Apache-2.0
// Recoverable Cost tile blanks (CTO-430). A blank has to carry the reason that is actually true:
// "no finding in this category" and "findings exist but none could be priced" are different
// answers, and the tile used to explain both with the second one.

import { render, screen } from "@testing-library/react";
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
  it("explains a category with no findings as an absence, not as a failure to price", () => {
    render(<WasteLive initialData={EMPTY} />);

    // The reason that does NOT apply: nothing failed to be bounded, there was nothing to bound.
    expect(screen.queryByText(/could not be bounded to a dollar amount/i)).toBeNull();
    // The reason that does.
    expect(
      screen.getAllByText(/no finding in this category, so there is no amount to recover/i).length,
    ).toBeGreaterThan(0);
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
  });
});
