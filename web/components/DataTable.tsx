// SPDX-License-Identifier: Apache-2.0
"use client";

// Shared dashboard table (CTO-177, plan workstream A1). Ten pages hand-roll the same <table>
// markup, so the visual conventions here are copied verbatim from them rather than reinvented:
// `w-full text-sm`, an uppercase `text-xs text-muted` header, `border-t border-edge` row rules,
// and `text-right tabular-nums` on numerics so digits line up column-wise.
//
// Sorting and pagination ship with the first version on purpose. Account lists are unbounded, and
// retrofitting paging into a component several pages already consume costs more than building it
// once. No page is migrated here; that is CTO-179.

import { useMemo, useState, type ReactNode } from "react";

export type SortDirection = "asc" | "desc";

/** What a cell can be sorted on. `null` means "no value", which always sorts last. */
export type SortValue = string | number | boolean | null | undefined;

export interface Column<Row> {
  /** Stable identity for the column. Also the sort key, so it must be unique within a table. */
  key: string;
  header: ReactNode;
  /** Numerics belong on the right; text on the left. Defaults to left. */
  align?: "left" | "right";
  render: (row: Row) => ReactNode;
  /**
   * Makes the column sortable. Omit it for columns with no meaningful order (a button, a
   * distribution sparkline), which then render as plain headers.
   */
  sortValue?: (row: Row) => SortValue;
  /** Extra classes on the <th>/<td>, for the `pl-3` gutters some existing tables use. */
  headerClassName?: string;
  cellClassName?: string;
}

export interface DataTableProps<Row> {
  columns: readonly Column<Row>[];
  rows: readonly Row[];
  /** React key per row. Index is a fallback only; prefer a real id from the row. */
  rowKey: (row: Row, index: number) => string;
  /** Shown in place of the body when there are no rows at all. */
  empty?: ReactNode;
  /** Rows per page. Pass 0 to render every row and hide the pager. */
  pageSize?: number;
  initialSort?: { key: string; direction: SortDirection };
  /** Per-row classes, for the locked/muted row treatment /unit-economics uses. */
  rowClassName?: (row: Row) => string;
}

interface SortState {
  key: string;
  direction: SortDirection;
}

const DEFAULT_PAGE_SIZE = 25;

/**
 * Ordering for one cell against another. Missing values sort last in both directions: an account
 * with no revenue wired is not "the cheapest", and floating it to the top of a descending sort
 * would read as a real zero.
 */
export function compareValues(a: SortValue, b: SortValue, direction: SortDirection): number {
  const aMissing = a === null || a === undefined;
  const bMissing = b === null || b === undefined;
  if (aMissing || bMissing) {
    if (aMissing && bMissing) return 0;
    return aMissing ? 1 : -1;
  }
  const flip = direction === "asc" ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") {
    if (Number.isNaN(a) || Number.isNaN(b)) {
      if (Number.isNaN(a) && Number.isNaN(b)) return 0;
      return Number.isNaN(a) ? 1 : -1;
    }
    return (a - b) * flip;
  }
  if (typeof a === "boolean" && typeof b === "boolean") {
    return (Number(a) - Number(b)) * flip;
  }
  return String(a).localeCompare(String(b)) * flip;
}

/**
 * Sorted copy of `rows`. Returns the input order untouched when the sort key names no sortable
 * column, so a stale saved sort can never blank or scramble a table. Array#sort is stable in every
 * engine we target, which keeps ties in their source order.
 */
export function sortRows<Row>(
  rows: readonly Row[],
  columns: readonly Column<Row>[],
  sort: SortState | null,
): Row[] {
  if (!sort) return [...rows];
  const column = columns.find((c) => c.key === sort.key);
  if (!column?.sortValue) return [...rows];
  const sortValue = column.sortValue;
  return [...rows].sort((a, b) => compareValues(sortValue(a), sortValue(b), sort.direction));
}

/** Total pages, never below 1 so "Page 1 of 1" holds for an empty table. */
export function pageCount(total: number, pageSize: number): number {
  if (pageSize <= 0) return 1;
  return Math.max(1, Math.ceil(total / pageSize));
}

/**
 * Page index clamped into range. Derived rather than stored, so shrinking the row set (a filter
 * upstream, a deleted account) cannot strand the viewer on a page that no longer exists.
 */
export function clampPage(page: number, total: number, pageSize: number): number {
  if (!Number.isFinite(page)) return 0;
  return Math.min(Math.max(0, Math.trunc(page)), pageCount(total, pageSize) - 1);
}

/** The slice of rows visible on `page`. `pageSize` of 0 means "everything on one page". */
export function pageSlice<Row>(rows: readonly Row[], page: number, pageSize: number): Row[] {
  if (pageSize <= 0) return [...rows];
  const safe = clampPage(page, rows.length, pageSize);
  return rows.slice(safe * pageSize, safe * pageSize + pageSize);
}

/**
 * Direction a fresh click on a column should apply. Right-aligned columns are money and counts,
 * where the interesting end is the top spender, so they open descending.
 */
export function nextSort<Row>(column: Column<Row>, current: SortState | null): SortState {
  if (current?.key === column.key) {
    return { key: column.key, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { key: column.key, direction: column.align === "right" ? "desc" : "asc" };
}

function SortArrow({ direction }: { direction: SortDirection | null }) {
  // The inactive arrow stays in the layout at low opacity so the header row does not reflow by a
  // few pixels every time the sort moves.
  return (
    <span aria-hidden className={direction ? "text-accent" : "opacity-0"}>
      {direction === "desc" ? "↓" : "↑"}
    </span>
  );
}

export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  empty = "No data yet.",
  pageSize = DEFAULT_PAGE_SIZE,
  initialSort,
  rowClassName,
}: DataTableProps<Row>) {
  const [sort, setSort] = useState<SortState | null>(initialSort ?? null);
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => sortRows(rows, columns, sort), [rows, columns, sort]);
  const totalPages = pageCount(sorted.length, pageSize);
  const safePage = clampPage(page, sorted.length, pageSize);
  const visible = pageSlice(sorted, safePage, pageSize);

  const showPager = pageSize > 0 && sorted.length > pageSize;
  const firstShown = sorted.length === 0 ? 0 : safePage * pageSize + 1;
  const lastShown = safePage * pageSize + visible.length;

  return (
    <div>
      {/* The table scrolls inside this box; the page body never scrolls sideways. */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              {columns.map((c) => {
                const align = c.align === "right" ? "text-right" : "text-left";
                const active = sort?.key === c.key ? sort.direction : null;
                const classes = `py-1 font-medium ${align} ${c.headerClassName ?? ""}`.trim();
                if (!c.sortValue) {
                  return (
                    <th key={c.key} scope="col" className={classes}>
                      {c.header}
                    </th>
                  );
                }
                return (
                  <th
                    key={c.key}
                    scope="col"
                    className={classes}
                    aria-sort={active === "asc" ? "ascending" : active === "desc" ? "descending" : "none"}
                  >
                    <button
                      type="button"
                      // Sorting restarts at page 1: staying on page 7 after a re-sort shows a
                      // slice the viewer never asked for.
                      onClick={() => {
                        setSort((current) => nextSort(c, current));
                        setPage(0);
                      }}
                      className={`inline-flex items-center gap-1 uppercase hover:text-fg ${
                        c.align === "right" ? "flex-row-reverse" : ""
                      }`}
                    >
                      <span>{c.header}</span>
                      <SortArrow direction={active} />
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr className="border-t border-edge">
                <td colSpan={columns.length} className="py-6 text-center text-sm text-muted">
                  {empty}
                </td>
              </tr>
            ) : (
              visible.map((row, i) => (
                <tr
                  key={rowKey(row, safePage * pageSize + i)}
                  className={`border-t border-edge ${rowClassName?.(row) ?? ""}`.trim()}
                >
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      className={`py-2 ${
                        c.align === "right" ? "text-right tabular-nums" : ""
                      } ${c.cellClassName ?? ""}`.trim()}
                    >
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {showPager ? (
        <div className="mt-3 flex items-center justify-between gap-3 text-xs text-muted">
          <span className="tabular-nums">
            {firstShown}–{lastShown} of {sorted.length}
          </span>
          <div className="flex items-center gap-2">
            <PagerButton disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>
              Previous
            </PagerButton>
            <span className="tabular-nums">
              Page {safePage + 1} of {totalPages}
            </span>
            <PagerButton
              disabled={safePage >= totalPages - 1}
              onClick={() => setPage(safePage + 1)}
            >
              Next
            </PagerButton>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function PagerButton({
  disabled,
  onClick,
  children,
}: {
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded-md border border-edge px-2 py-1 hover:text-fg disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-muted"
    >
      {children}
    </button>
  );
}
