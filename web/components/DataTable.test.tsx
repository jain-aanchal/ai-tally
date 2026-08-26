// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  type Column,
  DataTable,
  clampPage,
  compareValues,
  nextSort,
  pageCount,
  pageSlice,
  sortRows,
} from "./DataTable";

interface Account {
  id: string;
  name: string;
  costMicroUsd: number;
  revenueMicroUsd: number | null;
}

const COLUMNS: Column<Account>[] = [
  { key: "name", header: "Account", render: (r) => r.name, sortValue: (r) => r.name },
  {
    key: "cost",
    header: "Cost",
    align: "right",
    render: (r) => r.costMicroUsd,
    sortValue: (r) => r.costMicroUsd,
  },
  {
    key: "revenue",
    header: "Revenue",
    align: "right",
    render: (r) => r.revenueMicroUsd ?? "—",
    sortValue: (r) => r.revenueMicroUsd,
  },
  // Deliberately unsortable: stands in for the action / sparkline columns existing pages carry.
  { key: "actions", header: "Actions", render: () => "…" },
];

const ROWS: Account[] = [
  { id: "b", name: "beta", costMicroUsd: 300, revenueMicroUsd: null },
  { id: "a", name: "alpha", costMicroUsd: 100, revenueMicroUsd: 50 },
  { id: "c", name: "gamma", costMicroUsd: 200, revenueMicroUsd: 900 },
];

function makeRows(n: number): Account[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `acct-${i}`,
    name: `acct-${String(i).padStart(3, "0")}`,
    costMicroUsd: i,
    revenueMicroUsd: i,
  }));
}

/** Body cell text, page-visible only, as a grid. */
function bodyRows(): string[][] {
  const body = document.querySelectorAll("tbody tr");
  return Array.from(body).map((tr) => Array.from(tr.querySelectorAll("td")).map((td) => td.textContent ?? ""));
}

describe("compareValues", () => {
  it("orders numbers numerically rather than lexically", () => {
    expect(compareValues(9, 100, "asc")).toBeLessThan(0);
    expect(compareValues(9, 100, "desc")).toBeGreaterThan(0);
  });

  it("orders strings with localeCompare in both directions", () => {
    expect(compareValues("alpha", "beta", "asc")).toBeLessThan(0);
    expect(compareValues("alpha", "beta", "desc")).toBeGreaterThan(0);
  });

  it("sorts missing values last in BOTH directions (a blank is not a zero)", () => {
    expect(compareValues(null, 5, "asc")).toBeGreaterThan(0);
    expect(compareValues(null, 5, "desc")).toBeGreaterThan(0);
    expect(compareValues(5, undefined, "desc")).toBeLessThan(0);
    expect(compareValues(null, undefined, "asc")).toBe(0);
  });

  it("sorts NaN last as well", () => {
    expect(compareValues(Number.NaN, 1, "asc")).toBeGreaterThan(0);
    expect(compareValues(Number.NaN, 1, "desc")).toBeGreaterThan(0);
  });

  it("orders booleans false before true ascending", () => {
    expect(compareValues(false, true, "asc")).toBeLessThan(0);
    expect(compareValues(false, true, "desc")).toBeGreaterThan(0);
  });
});

describe("sortRows", () => {
  it("sorts ascending and descending on a numeric column", () => {
    const asc = sortRows(ROWS, COLUMNS, { key: "cost", direction: "asc" });
    expect(asc.map((r) => r.id)).toEqual(["a", "c", "b"]);
    const desc = sortRows(ROWS, COLUMNS, { key: "cost", direction: "desc" });
    expect(desc.map((r) => r.id)).toEqual(["b", "c", "a"]);
  });

  it("sorts a string column alphabetically", () => {
    expect(sortRows(ROWS, COLUMNS, { key: "name", direction: "asc" }).map((r) => r.id)).toEqual([
      "a",
      "b",
      "c",
    ]);
  });

  it("keeps rows with no value at the bottom when sorting descending", () => {
    const desc = sortRows(ROWS, COLUMNS, { key: "revenue", direction: "desc" });
    expect(desc.map((r) => r.id)).toEqual(["c", "a", "b"]);
  });

  it("leaves order untouched for an unsortable or unknown key", () => {
    expect(sortRows(ROWS, COLUMNS, { key: "actions", direction: "asc" }).map((r) => r.id)).toEqual([
      "b",
      "a",
      "c",
    ]);
    expect(sortRows(ROWS, COLUMNS, { key: "nope", direction: "asc" }).map((r) => r.id)).toEqual([
      "b",
      "a",
      "c",
    ]);
    expect(sortRows(ROWS, COLUMNS, null).map((r) => r.id)).toEqual(["b", "a", "c"]);
  });

  it("is stable on ties and does not mutate the input", () => {
    const tied: Account[] = [
      { id: "x", name: "x", costMicroUsd: 1, revenueMicroUsd: 0 },
      { id: "y", name: "y", costMicroUsd: 1, revenueMicroUsd: 0 },
      { id: "z", name: "z", costMicroUsd: 1, revenueMicroUsd: 0 },
    ];
    expect(sortRows(tied, COLUMNS, { key: "cost", direction: "desc" }).map((r) => r.id)).toEqual([
      "x",
      "y",
      "z",
    ]);
    sortRows(ROWS, COLUMNS, { key: "cost", direction: "asc" });
    expect(ROWS.map((r) => r.id)).toEqual(["b", "a", "c"]);
  });
});

describe("nextSort", () => {
  it("opens text columns ascending and numeric (right-aligned) columns descending", () => {
    expect(nextSort(COLUMNS[0], null)).toEqual({ key: "name", direction: "asc" });
    expect(nextSort(COLUMNS[1], null)).toEqual({ key: "cost", direction: "desc" });
  });

  it("toggles direction when the same column is clicked again", () => {
    expect(nextSort(COLUMNS[1], { key: "cost", direction: "desc" })).toEqual({
      key: "cost",
      direction: "asc",
    });
    expect(nextSort(COLUMNS[1], { key: "cost", direction: "asc" })).toEqual({
      key: "cost",
      direction: "desc",
    });
  });

  it("starts fresh when switching columns", () => {
    expect(nextSort(COLUMNS[0], { key: "cost", direction: "asc" })).toEqual({
      key: "name",
      direction: "asc",
    });
  });
});

describe("page boundaries", () => {
  it("counts pages, rounding a partial last page up", () => {
    expect(pageCount(0, 10)).toBe(1);
    expect(pageCount(10, 10)).toBe(1);
    expect(pageCount(11, 10)).toBe(2);
    expect(pageCount(25, 10)).toBe(3);
  });

  it("treats a page size of 0 as a single unpaged page", () => {
    expect(pageCount(500, 0)).toBe(1);
    expect(pageSlice(makeRows(5), 3, 0)).toHaveLength(5);
  });

  it("clamps a page index into range instead of stranding the viewer", () => {
    expect(clampPage(-4, 30, 10)).toBe(0);
    expect(clampPage(99, 30, 10)).toBe(2);
    expect(clampPage(1, 0, 10)).toBe(0);
    expect(clampPage(Number.NaN, 30, 10)).toBe(0);
  });

  it("slices exactly one page, with the remainder on the last page", () => {
    const rows = makeRows(25);
    expect(pageSlice(rows, 0, 10).map((r) => r.id)).toEqual(rows.slice(0, 10).map((r) => r.id));
    expect(pageSlice(rows, 1, 10).map((r) => r.id)).toEqual(rows.slice(10, 20).map((r) => r.id));
    expect(pageSlice(rows, 2, 10)).toHaveLength(5);
    // Past the end clamps back to the last page rather than returning nothing.
    expect(pageSlice(rows, 9, 10)).toHaveLength(5);
  });
});

describe("<DataTable />", () => {
  it("renders the shared header style and right-aligns numeric cells with tabular-nums", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(r) => r.id} />);
    const head = document.querySelector("thead");
    expect(head?.className).toContain("text-xs");
    expect(head?.className).toContain("uppercase");
    expect(head?.className).toContain("text-muted");

    const firstRow = document.querySelectorAll("tbody tr")[0];
    const cells = firstRow.querySelectorAll("td");
    expect(cells[0].className).not.toContain("text-right");
    expect(cells[1].className).toContain("text-right");
    expect(cells[1].className).toContain("tabular-nums");
  });

  it("keeps wide tables scrolling inside their own container", () => {
    const { container } = render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(r) => r.id} />);
    const wrapper = container.querySelector("table")?.parentElement;
    expect(wrapper?.className).toContain("overflow-x-auto");
  });

  it("shows the empty state spanning every column when there are no rows", () => {
    render(
      <DataTable columns={COLUMNS} rows={[]} rowKey={(r) => r.id} empty="No accounts yet." />,
    );
    const cell = screen.getByText("No accounts yet.");
    expect(cell.getAttribute("colspan")).toBe(String(COLUMNS.length));
  });

  it("sorts on header click and toggles on a second click", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(r) => r.id} />);
    expect(bodyRows().map((r) => r[0])).toEqual(["beta", "alpha", "gamma"]);

    // Cost is right-aligned, so the first click opens descending.
    fireEvent.click(screen.getByRole("button", { name: /Cost/ }));
    expect(bodyRows().map((r) => r[1])).toEqual(["300", "200", "100"]);

    fireEvent.click(screen.getByRole("button", { name: /Cost/ }));
    expect(bodyRows().map((r) => r[1])).toEqual(["100", "200", "300"]);
  });

  it("marks the sorted column with aria-sort and leaves unsortable columns as plain headers", () => {
    render(
      <DataTable
        columns={COLUMNS}
        rows={ROWS}
        rowKey={(r) => r.id}
        initialSort={{ key: "name", direction: "asc" }}
      />,
    );
    expect(bodyRows().map((r) => r[0])).toEqual(["alpha", "beta", "gamma"]);
    const headers = screen.getAllByRole("columnheader");
    expect(headers[0].getAttribute("aria-sort")).toBe("ascending");
    expect(headers[1].getAttribute("aria-sort")).toBe("none");
    expect(within(headers[3]).queryByRole("button")).toBeNull();
    expect(headers[3].getAttribute("aria-sort")).toBeNull();
  });

  it("paginates, and Previous/Next stop at the first and last page", () => {
    render(<DataTable columns={COLUMNS} rows={makeRows(25)} rowKey={(r) => r.id} pageSize={10} />);
    expect(bodyRows()).toHaveLength(10);
    expect(screen.getByText("Page 1 of 3")).toBeTruthy();
    expect(screen.getByText("1–10 of 25")).toBeTruthy();

    const prev = screen.getByRole("button", { name: "Previous" });
    const next = screen.getByRole("button", { name: "Next" });
    expect((prev as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(next);
    expect(bodyRows()[0][0]).toBe("acct-010");
    expect(screen.getByText("11–20 of 25")).toBeTruthy();
    expect((prev as HTMLButtonElement).disabled).toBe(false);

    // Last page holds the 5-row remainder and Next goes dead.
    fireEvent.click(next);
    expect(bodyRows()).toHaveLength(5);
    expect(screen.getByText("21–25 of 25")).toBeTruthy();
    expect((next as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(prev);
    expect(screen.getByText("Page 2 of 3")).toBeTruthy();
  });

  it("hides the pager when everything fits on one page", () => {
    render(<DataTable columns={COLUMNS} rows={makeRows(10)} rowKey={(r) => r.id} pageSize={10} />);
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
  });

  it("renders every row when pageSize is 0", () => {
    render(<DataTable columns={COLUMNS} rows={makeRows(40)} rowKey={(r) => r.id} pageSize={0} />);
    expect(bodyRows()).toHaveLength(40);
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
  });

  it("returns to page 1 when the sort changes, so the viewer sees the new top", () => {
    render(<DataTable columns={COLUMNS} rows={makeRows(25)} rowKey={(r) => r.id} pageSize={10} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Page 2 of 3")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Cost/ }));
    expect(screen.getByText("Page 1 of 3")).toBeTruthy();
    expect(bodyRows()[0][1]).toBe("24");
  });
});
