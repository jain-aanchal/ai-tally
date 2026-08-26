// SPDX-License-Identifier: Apache-2.0
"use client";

// The two DataTables on the account detail view (CTO-190, plan D4).
//
// Client module for the same reason AccountTable is one: `DataTable` takes a column spec whose
// `render` entries are functions, and functions cannot cross the server/client boundary. The page
// itself is an async server component, so the specs are declared on this side of the line.

import Link from "next/link";
import { useMemo } from "react";

import { DataTable, type Column } from "@/components/DataTable";
import { Blank, Money, Pct } from "@/components/HonestValue";
import type { AccountFeatureCost, AccountRunCost } from "@/lib/accounts";

/**
 * Top features by spend for this account.
 *
 * `share` is of the account's OWN direct spend, not the tenant's. The reader is asking "where does
 * this customer's money go", and a share of the tenant total would answer a question nobody on this
 * page is asking while looking exactly like the answer to the one they are.
 */
export function FeatureTable({
  features,
  accountDirectMicroUsd,
}: {
  features: readonly AccountFeatureCost[];
  accountDirectMicroUsd: number;
}) {
  const columns = useMemo<Column<AccountFeatureCost>[]>(
    () => [
      {
        key: "feature",
        header: "Feature",
        render: (r) => <span className="font-medium">{r.feature}</span>,
        sortValue: (r) => r.feature,
      },
      {
        key: "cost",
        header: "Direct cost",
        align: "right",
        render: (r) => <Money micro={r.directCostMicroUsd} />,
        sortValue: (r) => r.directCostMicroUsd,
      },
      {
        key: "share",
        header: "Share of account",
        align: "right",
        render: (r) =>
          accountDirectMicroUsd > 0 ? (
            <Pct value={r.directCostMicroUsd / accountDirectMicroUsd} />
          ) : (
            <Blank reason="this account has no directly attributable spend in the window, so there is no total to take a share of" />
          ),
        sortValue: (r) =>
          accountDirectMicroUsd > 0 ? r.directCostMicroUsd / accountDirectMicroUsd : null,
      },
      {
        key: "spans",
        header: "Spans",
        align: "right",
        render: (r) => r.spanCount.toLocaleString(),
        sortValue: (r) => r.spanCount,
      },
    ],
    [accountDirectMicroUsd],
  );

  return (
    <DataTable
      columns={columns}
      rows={features}
      rowKey={(r) => r.feature}
      pageSize={0}
      initialSort={{ key: "cost", direction: "desc" }}
      empty="No spans for this account carried a feature tag, so there is nothing to rank by feature."
    />
  );
}

/**
 * Heaviest agent runs attributed to this account.
 *
 * The cost column is the account's share of each run, not the run's total: see AccountRunCost. The
 * run id links to the existing /agents drill-down, which shows the WHOLE run, so the two figures
 * will differ for a run that served more than one customer. The column header and the note under
 * the table both say so, because a reader who clicks through and finds a bigger number has to be
 * able to tell "different scope" from "one of these is wrong".
 */
export function RunTable({ runs }: { runs: readonly AccountRunCost[] }) {
  const columns = useMemo<Column<AccountRunCost>[]>(
    () => [
      {
        key: "run",
        header: "Run",
        render: (r) => (
          <Link
            href={`/agents/runs/${encodeURIComponent(r.runId)}`}
            className="font-mono text-xs text-accent hover:underline"
          >
            {r.runId}
          </Link>
        ),
        sortValue: (r) => r.runId,
      },
      {
        key: "agent",
        header: "Agent",
        render: (r) => r.agent,
        sortValue: (r) => r.agent,
      },
      {
        key: "outcome",
        header: "Outcome",
        render: (r) => <OutcomeBadge outcome={r.outcome} />,
        sortValue: (r) => r.outcome,
      },
      {
        key: "steps",
        header: "Steps",
        align: "right",
        render: (r) => r.steps.toLocaleString(),
        sortValue: (r) => r.steps,
      },
      {
        key: "cost",
        header: "Cost to this account",
        align: "right",
        render: (r) => <Money micro={r.accountCostMicroUsd} />,
        sortValue: (r) => r.accountCostMicroUsd,
      },
    ],
    [],
  );

  return (
    <DataTable
      columns={columns}
      rows={runs}
      rowKey={(r) => r.runId}
      pageSize={0}
      initialSort={{ key: "cost", direction: "desc" }}
      empty="No agent runs in the window carried this account id."
    />
  );
}

function OutcomeBadge({ outcome }: { outcome: AccountRunCost["outcome"] }) {
  const cls = outcome === "failed" ? "bg-bad/20 text-bad" : "bg-good/20 text-good";
  return <span className={`rounded px-1.5 py-0.5 text-xs ${cls}`}>{outcome}</span>;
}
