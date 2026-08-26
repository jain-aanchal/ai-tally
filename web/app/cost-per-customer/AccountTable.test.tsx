// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { type AccountCostRow, emptyAccountRow } from "@/lib/accounts";
import { AccountTable } from "./AccountTable";

// The search box calls a server action. Mocked here so the test covers the matching rule (which is
// where the interesting bug lives) rather than the transport.
const lookup = vi.fn();
vi.mock("./actions", () => ({
  lookupAccountAction: (id: string) => lookup(id),
}));

const LABELLED = "1".repeat(64);
const UNLABELLED = "2".repeat(64);
/** The same account under the other spelling of the tenant id: a different HMAC key space. */
const OTHER_KEY_FORM = "3".repeat(64);

function row(hash: string, over: Partial<AccountCostRow> = {}): AccountCostRow {
  return {
    ...emptyAccountRow(hash, false),
    directCostMicroUsd: 2_000_000,
    distinctUsers: 4,
    spanCount: 500,
    ...over,
  };
}

function renderTable(
  rows: AccountCostRow[],
  labels: Record<string, string> = {},
  revenue: Record<string, number | null> = {},
  revenueUnavailable = false,
) {
  return render(
    <AccountTable
      rows={rows}
      labels={labels}
      labelsUnavailable={false}
      revenue={revenue}
      revenueUnavailable={revenueUnavailable}
      windowDays={30}
    />,
  );
}

describe("AccountTable", () => {
  beforeEach(() => {
    lookup.mockReset();
  });

  it("shows the label where one exists and a shortened hash where none does", () => {
    renderTable([row(LABELLED), row(UNLABELLED)], { [LABELLED]: "Acme Corp" });
    expect(screen.getByText("Acme Corp")).toBeTruthy();
    expect(screen.getByText(`${UNLABELLED.slice(0, 12)}…`)).toBeTruthy();
  });

  it("links each account to its detail view by the FULL hash, never the shortened form", () => {
    // The short form is a truncation for width and is not an id: a route built from it reaches no
    // account at all, so a link carrying it would 'work' visually and dead-end for every reader.
    renderTable([row(LABELLED), row(UNLABELLED)], { [LABELLED]: "Acme Corp" });
    expect(screen.getByText("Acme Corp").closest("a")?.getAttribute("href")).toBe(
      `/cost-per-customer/${LABELLED}`,
    );
    expect(
      screen.getByText(`${UNLABELLED.slice(0, 12)}…`).closest("a")?.getAttribute("href"),
    ).toBe(`/cost-per-customer/${UNLABELLED}`);
  });

  it("keeps the full hash reachable on hover and on copy", () => {
    renderTable([row(UNLABELLED)]);
    expect(screen.getByTitle(UNLABELLED)).toBeTruthy();
    expect(screen.getByTitle(`Copy the full account hash: ${UNLABELLED}`)).toBeTruthy();
  });

  it("blanks cost per user below the sample floor instead of printing a noisy ratio", () => {
    renderTable([row(UNLABELLED, { spanCount: 3, distinctUsers: 1 })]);
    expect(screen.getByText(/No value: needs ≥50 spans/i)).toBeTruthy();
  });

  it("matches a searched account on any of the candidate hashes, not just the first", async () => {
    // The row was ingested under the second spelling of the tenant id. Testing only hashes[0]
    // would report a real, spending customer as having no spend.
    lookup.mockResolvedValue({ ok: true, hashes: [UNLABELLED, OTHER_KEY_FORM] });
    renderTable([row(LABELLED), row(OTHER_KEY_FORM)], { [LABELLED]: "Acme Corp" });

    fireEvent.change(screen.getByLabelText("Find account"), { target: { value: "acme-corp" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(screen.getByText(/Showing the matching account/)).toBeTruthy());
    // Filtered down to the matched row: the other account is gone from the table.
    expect(screen.queryByText("Acme Corp")).toBeNull();
  });

  it("says an account has no spend rather than emptying the table", async () => {
    lookup.mockResolvedValue({ ok: true, hashes: ["9".repeat(64)] });
    renderTable([row(LABELLED)], { [LABELLED]: "Acme Corp" });

    fireEvent.change(screen.getByLabelText("Find account"), { target: { value: "ghost" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(screen.getByText(/no directly attributable spend in the last 30 days/)).toBeTruthy(),
    );
    expect(screen.getByText("Acme Corp")).toBeTruthy();
  });

  it("surfaces a lookup failure without clearing the table", async () => {
    lookup.mockResolvedValue({ ok: false, error: "Account lookup unavailable: timeout" });
    renderTable([row(LABELLED)], { [LABELLED]: "Acme Corp" });

    fireEvent.change(screen.getByLabelText("Find account"), { target: { value: "acme-corp" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(screen.getByText(/Account lookup unavailable/)).toBeTruthy());
    expect(screen.getByText("Acme Corp")).toBeTruthy();
  });

  // --- Revenue and margin (CTO-197, plan E4) ----------------------------------------------------

  it("prints a measured zero as $0.00 and an unknown as a blank, never the same cell", () => {
    renderTable(
      [row(LABELLED, { directCostMicroUsd: 1_000_000 }), row(UNLABELLED)],
      { [LABELLED]: "Netted Out", [UNLABELLED]: "No Source" },
      // Netted to zero by a refund: a measurement. Absent from the record: an unknown.
      { [LABELLED]: 0 },
    );

    // The zero-revenue account: revenue $0.00, margin minus its cost. Both are real numbers.
    expect(screen.getByText("$0.00")).toBeTruthy();
    expect(screen.getByText("-$1.00")).toBeTruthy();
    // The unknown account gets a blank that says why, and no invented margin of minus cost.
    expect(screen.getAllByText(/No value: no revenue source wired/i).length).toBe(2);
  });

  it("distinguishes an unreadable revenue source from an unwired one", () => {
    renderTable([row(LABELLED)], {}, {}, true);
    expect(screen.getAllByText(/No value: revenue could not be read/i).length).toBe(2);
    expect(screen.queryByText(/no revenue source wired/i)).toBeNull();
  });

  it("marks a margin whose cost side is too thin to stand behind", () => {
    // The shape of the current tenant: real revenue, a handful of attributed spans.
    renderTable(
      [row(LABELLED, { spanCount: 4, directCostMicroUsd: 130 })],
      {},
      { [LABELLED]: 20_000_000_000 },
    );
    const mark = screen.getByTestId(`margin-caveat-${LABELLED}`);
    expect(mark.getAttribute("title")).toMatch(/compute and egress are excluded/i);
    expect(mark.getAttribute("title")).toMatch(/below the 50-span floor/i);
    expect(mark.getAttribute("title")).toMatch(/under 1% of revenue/i);
  });

  it("still marks a well-measured margin, because v1 excludes compute and egress for everyone", () => {
    renderTable(
      [row(LABELLED, { spanCount: 5_000, directCostMicroUsd: 4_000_000 })],
      {},
      { [LABELLED]: 10_000_000 },
    );
    const mark = screen.getByTestId(`margin-caveat-${LABELLED}`);
    expect(mark.getAttribute("title")).toMatch(/compute and egress are excluded/i);
    expect(mark.getAttribute("title")).not.toMatch(/span floor/i);
  });

  it("ranks by margin and keeps unknown-revenue accounts at the bottom of both directions", () => {
    const LOSS = "4".repeat(64);
    renderTable(
      [
        row(UNLABELLED, { directCostMicroUsd: 1_000_000 }),
        row(LABELLED, { directCostMicroUsd: 1_000_000 }),
        row(LOSS, { directCostMicroUsd: 9_000_000 }),
      ],
      { [LABELLED]: "Profitable", [UNLABELLED]: "Unknown", [LOSS]: "Loss Maker" },
      { [LABELLED]: 5_000_000, [LOSS]: 1_000_000 },
    );

    const names = () =>
      screen.getAllByRole("row").slice(1).map((r) => r.querySelector("td")?.textContent ?? "");

    // Default sort is margin descending: most profitable first, unknown last.
    expect(names()[0]).toMatch(/Profitable/);
    expect(names()[2]).toMatch(/Unknown/);

    fireEvent.click(screen.getByRole("button", { name: /Gross margin/i }));
    // Ascending puts the customer losing money first, and the unknown STAYS last: it is neither the
    // most nor the least profitable customer, because it is not a measurement at all.
    expect(names()[0]).toMatch(/Loss Maker/);
    expect(names()[2]).toMatch(/Unknown/);
  });
});
