// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /waste (CTO-235, W8 of epic CTO-227), on the interactive design
// foundation (CTO-224 primitives: PageHeader + FilterBar + SummaryTile + DataTable + honest values).
//
// The server component owns the first paint; here we re-derive the endpoint from useFilters so a
// window or dimension change re-queries, and useLivePoll refreshes it on an interval (mirrors
// agents/Live.tsx). Every dollar renders through <Money>: a finding that cannot bound its recoverable
// spend is a null, shown as an honest <Blank> with a reason, NEVER $0 (CTO-227 honesty posture) --
// a guessed zero would read as "nothing to save here", the opposite of an un-quantifiable finding.

"use client";

import Link from "next/link";
import { Suspense, useMemo } from "react";

import { Card } from "@/components/Card";
import { DataTable, type Column } from "@/components/DataTable";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { Blank, Money } from "@/components/HonestValue";
import { LiveIndicator } from "@/components/LiveIndicator";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import { useFilters } from "@/lib/useFilters";
import { useLivePoll } from "@/lib/useLivePoll";
import {
  WASTE_CATEGORIES,
  type WasteCategory,
  type WasteConfidence,
  type WasteFinding,
  type WasteReport,
} from "@/lib/waste";

// Readable labels for the closed WasteCategory union (CTO-235). Kept local, per the ticket, so the
// pure lib/waste.ts stays free of presentation. The union is closed, so this Record is exhaustive and
// a new category would force a compile error here until it is labelled.
const CATEGORY_LABEL: Record<WasteCategory, string> = {
  paid_for_nothing: "Failed but billed",
  duplicated_work: "Duplicated work",
  wrong_sized_model: "Wrong-sized model",
  no_measured_return: "Unproven spend",
  structural_inefficiency: "Structural inefficiency",
};

// Why a per-category tile / the headline can be blank: at least one finding may exist yet none could
// put a defensible dollar bound on what stopping it recovers. That is a null all the way through the
// report (CTO-227), rendered as an honest blank rather than $0.
const NO_BOUNDED_TOTAL = "no finding could be bounded to a dollar amount";
const NO_BOUNDED_CATEGORY = "no finding in this category could be bounded to a dollar amount";

export function WasteLive({ initialData }: { initialData: WasteReport }) {
  // The FilterBar writes the window + dimension filters to the URL; useFilters reads them back as a
  // query string that rides onto /api/waste, so flipping 7d/30d/90d or a feature/model filter
  // re-parameterises the endpoint and useLivePoll re-fetches the re-windowed report (CTO-235).
  const { queryString } = useFilters();
  const endpoint = queryString ? `/api/waste?${queryString}` : "/api/waste";
  const { data: report, updatedAt } = useLivePoll<WasteReport>(endpoint, initialData);

  const findings = report.findings;

  // Filter options come from the findings themselves (their scopeValues), not a separate fetch: the
  // report is the only data this page has, and a dimension with no findings simply offers no control.
  const featureOptions = scopeOptions(findings, "feature");
  const modelOptions = scopeOptions(findings, "model");

  // Per-category finding counts, for the tile hints. A category with no findings still gets a key.
  const countByCategory = useMemo(() => {
    const counts = {} as Record<WasteCategory, number>;
    for (const c of WASTE_CATEGORIES) counts[c] = 0;
    for (const f of findings) counts[f.category] += 1;
    return counts;
  }, [findings]);

  const columns = useMemo<Column<WasteFinding>[]>(
    () => [
      {
        key: "category",
        header: "Category",
        sortValue: (f) => CATEGORY_LABEL[f.category],
        render: (f) => <span className="font-medium">{CATEGORY_LABEL[f.category]}</span>,
      },
      {
        key: "where",
        header: "Where",
        sortValue: (f) => f.scopeValue,
        render: (f) => (
          <span className="flex items-center gap-1.5">
            <span className="rounded bg-ink px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
              {f.scopeKind}
            </span>
            {f.drillHref ? (
              <Link href={f.drillHref} className="font-mono text-accent hover:underline">
                {f.scopeValue}
              </Link>
            ) : (
              <span className="font-mono">{f.scopeValue}</span>
            )}
          </span>
        ),
      },
      {
        key: "recoverable",
        header: "Recoverable",
        align: "right",
        cellClassName: "font-semibold",
        // Nulls sort last in DataTable, so the biggest bounded opportunities lead and the
        // un-quantifiable ones trail -- matching aggregateWaste's own ordering.
        sortValue: (f) => f.recoverableMicroUsd,
        render: (f) => (
          // A null recoverable is an honest blank with a reason, NEVER $0 (CTO-227).
          <Money
            micro={f.recoverableMicroUsd}
            reason="this finding is real but its recoverable spend could not be defensibly bounded"
          />
        ),
      },
      {
        key: "windowSpend",
        header: "Window spend",
        align: "right",
        cellClassName: "text-muted",
        sortValue: (f) => f.windowSpendMicroUsd,
        // Always known (observed spend on the scope over the window), so no blank branch here.
        render: (f) => <Money micro={f.windowSpendMicroUsd} />,
      },
      {
        key: "confidence",
        header: "Confidence",
        // Sort high -> medium -> low by an explicit rank, not the string, so the order is meaningful.
        sortValue: (f) => confidenceRank(f.confidence),
        render: (f) => <ConfidenceBadge confidence={f.confidence} />,
      },
      {
        key: "reason",
        header: "Reason",
        headerClassName: "pl-4",
        cellClassName: "pl-4 text-muted max-w-md",
        render: (f) => <span>{f.reason}</span>,
      },
    ],
    [],
  );

  const body = (
    <div className="space-y-6">
      <TileGrid>
        <SummaryTile
          label="Recoverable"
          micro={report.totalRecoverableMicroUsd}
          reason={NO_BOUNDED_TOTAL}
          // The total is over the SELECTED window, not a month -- label it with the real day count.
          hint={`last ${report.generatedForWindowDays} days · ${findings.length} finding${
            findings.length === 1 ? "" : "s"
          }`}
        />
        {WASTE_CATEGORIES.map((c) => (
          <SummaryTile
            key={c}
            label={CATEGORY_LABEL[c]}
            micro={report.byCategory[c]}
            reason={NO_BOUNDED_CATEGORY}
            hint={`${countByCategory[c]} finding${countByCategory[c] === 1 ? "" : "s"}`}
          />
        ))}
      </TileGrid>

      <Card title="Findings — hypotheses with evidence">
        {report.unavailable ? (
          // Hard failure: the report could not be produced. Render the honest reason, not an empty
          // table that would read as "no waste" (CTO-227).
          <p className="text-sm">
            <Blank reason={report.unavailable} /> the waste report is unavailable for this window.
          </p>
        ) : findings.length === 0 ? (
          // GOOD news, not a broken page: nothing recoverable was detected in this window.
          <p className="text-sm text-good">
            No recoverable waste found in this window. Every detector ran and flagged nothing.
          </p>
        ) : (
          <DataTable
            columns={columns}
            rows={findings}
            rowKey={(f, i) => `${f.category}:${f.scopeKind}:${f.scopeValue}:${f.title}:${i}`}
            initialSort={{ key: "recoverable", direction: "desc" }}
            pageSize={25}
          />
        )}
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Recoverable Cost"
        subtitle="Recoverable AI spend: findings are hypotheses with evidence."
        actions={<LiveIndicator updatedAt={updatedAt} />}
        toolbar={
          <Suspense fallback={null}>
            <FilterBar
              hideGroupBy
              options={{ feature: featureOptions, model: modelOptions }}
            />
          </Suspense>
        }
      />
      {body}
    </div>
  );
}

/** Distinct scopeValues for a dimension across the findings, as FilterBar options (dedup, in order). */
function scopeOptions(findings: WasteFinding[], kind: WasteFinding["scopeKind"]): FilterOption[] {
  const seen = new Set<string>();
  const out: FilterOption[] = [];
  for (const f of findings) {
    if (f.scopeKind !== kind || seen.has(f.scopeValue)) continue;
    seen.add(f.scopeValue);
    out.push({ value: f.scopeValue });
  }
  return out;
}

/** Rank for sorting: a higher number is more confident, so a descending sort leads with high. */
function confidenceRank(c: WasteConfidence): number {
  return c === "high" ? 3 : c === "medium" ? 2 : 1;
}

/**
 * Confidence badge. Confidence is trust in the FINDING, not a severity score, so none of the tones is
 * green: a low-confidence finding is a caveat ("we are less sure this is waste"), and must NOT read as
 * a clean bill of health (CTO-235). High is the strongest signal, low the most tentative.
 */
function ConfidenceBadge({ confidence }: { confidence: WasteConfidence }) {
  const cls =
    confidence === "high"
      ? "bg-bad/20 text-bad"
      : confidence === "medium"
        ? "bg-warn/20 text-warn"
        : "border border-dashed border-edge text-muted";
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium ${cls}`}>
      {confidence}
    </span>
  );
}
