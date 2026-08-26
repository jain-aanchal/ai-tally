// SPDX-License-Identifier: Apache-2.0
"use client";

// The /cost-per-customer account table and its search box (CTO-188, plan D2).
//
// Client module for the same reason ConnectorTable is one: `DataTable` takes a column spec whose
// `render` entries are functions, and functions cannot cross the server/client boundary. page.tsx
// is an async server component, so the spec is declared on this side of the line.

import { useMemo, useState, useTransition } from "react";

import { DataTable, type Column } from "@/components/DataTable";
import { Blank, Money } from "@/components/HonestValue";
import {
  type AccountCostRow,
  type AccountMargin,
  accountMargin,
  costPerUser,
  shortenAccountHash,
} from "@/lib/accounts";
import { lookupAccountAction } from "./actions";

interface SearchState {
  /** Hashes the searched id could have been emitted under. Empty until a search succeeds. */
  matched: string[];
  /** Whether any of those hashes is actually a row in the window. */
  found: boolean;
  message: string;
  tone: "ok" | "warn" | "err";
}

export function AccountTable({
  rows,
  labels,
  labelsUnavailable,
  revenue,
  revenueUnavailable,
  windowDays,
}: {
  rows: readonly AccountCostRow[];
  /**
   * Hash to label. A plain record rather than a Map because this crosses the serialization
   * boundary from a server component.
   */
  labels: Record<string, string>;
  /** True when the gateway could not be reached, so an unlabelled row is not proof of no label. */
  labelsUnavailable: boolean;
  /**
   * Hash to net revenue in micro-USD, for accounts that have one (CTO-197, plan E4).
   *
   * A missing key and an explicit `null` mean the same thing and both render blank: we have not
   * been told this account's revenue. A `0` is a measurement (a charge fully netted by a refund)
   * and prints as $0.00. Nothing here may treat the two alike.
   */
  revenue: Record<string, number | null>;
  /** True when the revenue read failed, so a blank is not proof that no revenue source is wired. */
  revenueUnavailable: boolean;
  windowDays: number;
}) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<SearchState | null>(null);
  const [pending, startTransition] = useTransition();

  const matchedSet = useMemo(
    () => new Set(search?.found ? search.matched : []),
    [search],
  );

  // A found account filters the table down to its row, which is how you "jump" to a row inside a
  // sorted, paginated table: the row you asked for may be on page 14 of an unbounded list, and
  // scrolling to an offset that a re-sort invalidates would be worse than showing it alone.
  // A search that matched nothing does NOT filter: an empty table would read as "you broke it"
  // when the true statement is "this account has no spend in the window".
  const visibleRows = useMemo(
    () => (matchedSet.size > 0 ? rows.filter((r) => matchedSet.has(r.accountIdHash)) : rows),
    [rows, matchedSet],
  );

  const runSearch = (raw: string) => {
    const trimmed = raw.trim();
    if (!trimmed) {
      setSearch(null);
      return;
    }
    startTransition(async () => {
      const result = await lookupAccountAction(trimmed);
      if (!result.ok) {
        setSearch({ matched: [], found: false, message: result.error, tone: "err" });
        return;
      }
      // Match against the WHOLE set of candidate hashes. The tenant identifier is HMAC key
      // material, so the same account has a different digest per spelling of the tenant id and the
      // endpoint refuses to canonicalize. Testing only the first would miss the account outright
      // whenever its spans were ingested under the other spelling.
      const hit = rows.some((r) => result.hashes.includes(r.accountIdHash));
      setSearch({
        matched: result.hashes,
        found: hit,
        message: hit
          ? "Showing the matching account. Clear the search to see every account again."
          : `That account id hashes to a valid account, but it has no directly attributable spend in the last ${windowDays} days.`,
        tone: hit ? "ok" : "warn",
      });
    });
  };

  // One place that pairs a row with its revenue, so the render path and the sort path can never
  // disagree about what an account earns. `revenue[hash]` is `undefined` for an account the revenue
  // query returned no row for, which means exactly what an explicit `null` means: unknown.
  const margin = useMemo(
    () => (r: AccountCostRow) =>
      accountMargin(r, revenue[r.accountIdHash] ?? null, revenueUnavailable),
    [revenue, revenueUnavailable],
  );

  const columns = useMemo<Column<AccountCostRow>[]>(
    () => [
      {
        key: "account",
        header: "Account",
        render: (r) => (
          <AccountCell
            accountIdHash={r.accountIdHash}
            label={labels[r.accountIdHash]}
            labelsUnavailable={labelsUnavailable}
          />
        ),
        // Sorts by what the reader can see: labelled accounts read alphabetically, unlabelled ones
        // by hash. Sorting by the hash under a label would look like no sort at all.
        sortValue: (r) => labels[r.accountIdHash] ?? r.accountIdHash,
      },
      {
        key: "users",
        header: "Users",
        align: "right",
        render: (r) =>
          r.distinctUsers > 0 ? (
            r.distinctUsers.toLocaleString()
          ) : (
            <Blank reason="no user id was recorded on this account's spans, so distinct users cannot be counted" />
          ),
        sortValue: (r) => r.distinctUsers,
      },
      {
        key: "cost",
        header: "Direct cost",
        align: "right",
        render: (r) => <Money micro={r.directCostMicroUsd} />,
        sortValue: (r) => r.directCostMicroUsd,
      },
      {
        key: "costPerUser",
        header: "Cost per user",
        align: "right",
        render: (r) => {
          const { micro, reason } = costPerUser(r);
          return micro === null ? (
            <Blank reason={reason ?? "not enough data to divide cost by users"} />
          ) : (
            <Money micro={micro} />
          );
        },
        // Low-sample rows carry no value, and `null` sorts last in both directions by design, so a
        // suppressed account never floats to the top of a "cheapest per user" sort.
        sortValue: (r) => costPerUser(r).micro,
      },
      {
        key: "revenue",
        header: "Revenue",
        align: "right",
        render: (r) => {
          const { revenueMicroUsd, reason } = margin(r);
          // `0` is a real measurement and prints as $0.00; `null` is an absence and prints blank.
          // Money's own null path would blur that, so the branch is explicit here.
          return revenueMicroUsd === null ? (
            <Blank reason={reason ?? "revenue unknown for this account"} />
          ) : (
            <Money micro={revenueMicroUsd} />
          );
        },
        sortValue: (r) => margin(r).revenueMicroUsd,
      },
      {
        key: "margin",
        header: "Gross margin",
        align: "right",
        render: (r) => <MarginCell row={r} margin={margin(r)} />,
        // Unknown revenue means unknown margin, and `null` sorts last in both directions, so an
        // account we know nothing about never ranks as the most OR the least profitable customer.
        sortValue: (r) => margin(r).marginMicroUsd,
      },
    ],
    [labels, labelsUnavailable, margin],
  );

  return (
    <div className="space-y-3">
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          runSearch(query);
        }}
      >
        <label htmlFor="account-search" className="text-xs uppercase tracking-wide text-muted">
          Find account
        </label>
        <input
          id="account-search"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="your own account id, e.g. acme-corp"
          className="min-w-64 flex-1 rounded-md border border-edge bg-ink/40 px-3 py-1.5 text-sm placeholder:text-muted focus:border-accent/60 focus:outline-none"
        />
        <button
          type="submit"
          disabled={pending}
          className="rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
        >
          {pending ? "Hashing…" : "Search"}
        </button>
        {search ? (
          <button
            type="button"
            onClick={() => {
              setQuery("");
              setSearch(null);
            }}
            className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:text-white"
          >
            Clear
          </button>
        ) : null}
      </form>

      <p className="max-w-prose text-xs text-muted">
        The id is hashed with this tenant&apos;s own key to find its row. It is never stored, logged,
        or shown back to you, which is also why there is no way to search the other direction.
      </p>

      {search ? (
        <p
          className={`text-xs ${
            search.tone === "ok" ? "text-good" : search.tone === "warn" ? "text-warn" : "text-warn"
          }`}
        >
          {search.message}
        </p>
      ) : null}

      <DataTable
        columns={columns}
        rows={visibleRows}
        rowKey={(r) => r.accountIdHash}
        // Ranked by profitability, most profitable first, which is the question this tab exists to
        // answer. Sorting ascending puts the customers losing money at the top; accounts with
        // unknown revenue stay at the bottom either way rather than posing as the answer.
        initialSort={{ key: "margin", direction: "desc" }}
        // A row the search found is filtered to on its own, so the highlight is belt and braces for
        // the case where a tenant later labels two hashes of the same account and both rows show.
        rowClassName={(r) => (matchedSet.has(r.accountIdHash) ? "bg-accent/5" : "")}
        // Deliberately plain. The onboarding empty state that explains what this page is for and
        // how to switch it on is CTO-191, stacked on this ticket; a half-built version here would
        // be the thing that ticket then has to unpick.
        empty={`No spans carried an account id in the last ${windowDays} days.`}
      />

      {/* The margin column's own caveat, kept next to the column rather than only at the top of the
          page. The tenant-wide excluded-cost banner with the real dollar figure is CTO-189, running
          concurrently; when it lands this line can point at it instead of restating it. Until then
          a profitability ranking would otherwise sit here with nothing beside it saying that half
          the cost base is missing. */}
      <p className="max-w-prose text-xs text-warn">
        Gross margin is revenue minus <em>direct</em> cost only. Compute and egress are excluded
        from every account, so cost is understated and every margin above is overstated by the same
        amount. Rows marked ▲ carry a further reason not to read them at face value; hover the mark
        to see it. Ranking by this column tells you the order to look in, not what a customer
        actually earns you.
      </p>
    </div>
  );
}

/**
 * The Gross margin cell: revenue minus direct cost, with the reasons it cannot be taken at face
 * value attached to the number itself.
 *
 * The caveat marker is not decoration. Every margin in v1 is overstated because compute and egress
 * are excluded from the cost side, and on a lightly-instrumented account it is overstated by so
 * much that the figure is really just revenue. The page header says this too, but the header
 * scrolls away and this is the cell someone screenshots into a pricing discussion.
 */
function MarginCell({ row, margin }: { row: AccountCostRow; margin: AccountMargin }) {
  if (margin.marginMicroUsd === null) {
    return <Blank reason={margin.reason ?? "margin unknown for this account"} />;
  }
  const negative = margin.marginMicroUsd < 0;
  return (
    <span className="inline-flex items-center justify-end gap-1">
      <Money
        micro={margin.marginMicroUsd}
        className={negative ? "text-warn" : undefined}
      />
      {margin.caveats.length > 0 ? (
        <span
          title={margin.caveats.join("\n\n")}
          className="cursor-help text-[11px] text-warn"
          data-testid={`margin-caveat-${row.accountIdHash}`}
        >
          <span aria-hidden>▲</span>
          <span className="sr-only">
            Read with care: {margin.caveats.join(" ")}
          </span>
        </span>
      ) : null}
    </span>
  );
}

/**
 * The Account cell: label where the tenant set one, shortened hash otherwise, full hash on hover
 * and on copy.
 *
 * The full hash is what every other surface takes (the label API, a support conversation), so it
 * has to be retrievable from the row. The short form is for width only.
 */
function AccountCell({
  accountIdHash,
  label,
  labelsUnavailable,
}: {
  accountIdHash: string;
  label: string | undefined;
  labelsUnavailable: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(accountIdHash);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be refused (insecure origin, denied permission). The hash is already
      // selectable in the title attribute, so there is nothing to recover and nothing to shout at
      // the user about.
      setCopied(false);
    }
  };

  return (
    <span className="inline-flex items-center gap-2">
      <span title={accountIdHash} className={label ? "font-medium" : "font-mono text-xs"}>
        {label ?? shortenAccountHash(accountIdHash)}
      </span>
      {!label && !labelsUnavailable ? (
        <span className="text-[11px] text-muted" title="no label set for this account">
          unlabelled
        </span>
      ) : null}
      <button
        type="button"
        onClick={copy}
        title={`Copy the full account hash: ${accountIdHash}`}
        className="rounded border border-edge px-1.5 py-0.5 text-[11px] text-muted hover:text-white"
      >
        {copied ? "Copied" : "Copy hash"}
      </button>
    </span>
  );
}
