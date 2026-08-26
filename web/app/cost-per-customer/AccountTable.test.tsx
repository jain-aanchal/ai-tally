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

function renderTable(rows: AccountCostRow[], labels: Record<string, string> = {}) {
  return render(
    <AccountTable rows={rows} labels={labels} labelsUnavailable={false} windowDays={30} />,
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
});
