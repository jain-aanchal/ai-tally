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
    ],
    [labels, labelsUnavailable],
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
        initialSort={{ key: "cost", direction: "desc" }}
        // A row the search found is filtered to on its own, so the highlight is belt and braces for
        // the case where a tenant later labels two hashes of the same account and both rows show.
        rowClassName={(r) => (matchedSet.has(r.accountIdHash) ? "bg-accent/5" : "")}
        // Deliberately plain, and unreachable from the page as it stands: with no rows at all the
        // page renders the CTO-191 onboarding explainer in place of this table, and a search that
        // matches nothing does not filter. This stays as the honest fallback for any future caller
        // that renders the table without that branch.
        empty={`No spans carried an account id in the last ${windowDays} days.`}
      />
    </div>
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
