// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /attribution (CTO-108).
//
// Migrated onto the shared primitives in CTO-179: the per-provider table is a `DataTable` column
// spec, and every number goes through `<Money>` / `<Pct>` so a blank cell carries the reason it is
// blank. Value/user and margin/user are the blanks that matter here. They are empty because no
// revenue source is wired for the tenant, not because those providers earn nothing, and until now
// the page rendered a bare glyph that read as a bug.
//
// CTO-429: the Cost column and the Total cost tile no longer format a NULL-skipping sum. A system
// whose spans all lack a catalog rate summed to 0 and rendered a confident "$0.00" under a footnote
// asserting every row was real spend. The decision lives in ./costCoverage; the row and the totals
// now carry the unpriced/total span counts that make it, and everything derived from a cost that
// cannot be shown blanks with the cost's own reason instead of dividing by a fabricated zero.
//
// CTO-223 rebuilds the page onto the design foundation: a `PageHeader` + `FilterBar` (time range,
// provider, feature) drives the window and slice, the four headline metrics are `SummaryTile`s, and
// the provider breakdown is an `InteractiveStackedChart` of daily LLM cost per provider. None of the
// numbers, columns, or honest-blank rules change; this is a design + interactivity pass.

"use client";

import { useMemo } from "react";

import { Card } from "@/components/Card";
import {
  NoDataYet,
  SourceUnavailable,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { DataTable, type Column } from "@/components/DataTable";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { Money, Pct } from "@/components/HonestValue";
import { InteractiveStackedChart, type StackedChartDay } from "@/components/InteractiveStackedChart";
import { LiveIndicator } from "@/components/LiveIndicator";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import { type AttributionReport, type ProviderAttribution, systemKind } from "@/lib/attribution";
import type { SourceState } from "@/lib/dataState";
import { useLivePoll } from "@/lib/useLivePoll";
import { costCoverage } from "./costCoverage";

/**
 * Why value/user is blank. `buildProviderRow` only fills it when monetary `business_events` exist
 * for the provider, and the tenant's revenue source has to be configured for any to arrive. That
 * configuration is workstream E of the cost-per-customer plan, so today the honest answer is "no
 * revenue is wired", never "$0".
 */
const NO_REVENUE_WIRED =
  "no revenue source is wired for this tenant, so there is no revenue to divide across users";

/** Margin is value/user minus cost/user, so it inherits the missing half of the subtraction. */
const NO_REVENUE_FOR_MARGIN =
  "margin needs revenue: no revenue source is wired for this tenant, so only the cost side is known";

/**
 * Why everything derived from an unpriced row is blank too (CTO-429).
 *
 * `buildProviderRow` already nulls the ratios when a span went unpriced, but the page explained
 * those blanks with the OTHER reason each of them can have ("no conversion events", "no revenue
 * wired"), which is a wrong answer rather than a missing one. A reader hovering a blank on a row
 * whose cost is itself unknown needs to be sent to the cost, not to a conversion count that is
 * fine.
 */
const COST_UNKNOWN_FOR_RATIO =
  "the cost for this system is unknown, not zero: some of its spans carry no catalog rate, so anything divided by that cost is unknown too";

/** The scope phrase costCoverage() completes, for a single table row. */
const ROW_SCOPE = "for this system in this window";

/**
 * The LLM providers the `?provider=` filter accepts (see parseFilters). Narrower than what the table
 * can show: the breakdown dimension is `gen_ai.system`, so vector vendors appear as rows even though
 * they are not filter options (#320).
 */
const PROVIDER_OPTIONS: FilterOption[] = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
];

/** The report plus which of the four source states produced it (see lib/dataState.ts). */
export type AttributionPayload = AttributionReport & { state: SourceState };

export function AttributionLive({
  endpoint,
  initialData,
  outcome,
  featureTags,
}: {
  endpoint: string;
  initialData: AttributionPayload;
  outcome: string;
  /** Feature tags in the window, for the FilterBar's feature filter. Empty renders no such control. */
  featureTags: string[];
}) {
  const { data: report, updatedAt } = useLivePoll<AttributionPayload>(endpoint, initialData);

  // Columns close over `outcome`, which comes from the URL filters, so they are rebuilt only when
  // the filter changes rather than on every poll tick.
  const columns = useMemo<Column<ProviderAttribution>[]>(
    () => [
      {
        key: "provider",
        header: "System",
        cellClassName: "font-mono",
        // #320: the breakdown key is `gen_ai.system`, which is the LLM provider on an LLM span and
        // the vector vendor on a vector span, so pinecone/weaviate/qdrant legitimately appear here.
        // Calling the column "Provider" made those rows read as LLM providers. The column is named
        // for what it holds, and a vector row is tagged so nobody has to recognise the vendor.
        render: (p) => (
          <>
            {p.provider}
            {systemKind(p.provider) === "vector" && (
              <span
                className="ml-2 rounded border border-edge px-1 py-0.5 text-[10px] uppercase tracking-wide text-muted"
                title="a vector store, not an LLM provider: gen_ai.system on a vector span names the vector vendor"
              >
                vector
              </span>
            )}
          </>
        ),
      },
      {
        key: "sessions",
        header: "Sessions",
        align: "right",
        render: (p) => p.sessions.toLocaleString(),
      },
      {
        key: "conversions",
        header: `${outcome}s`,
        align: "right",
        render: (p) => p.conversions.toLocaleString(),
      },
      {
        key: "rate",
        header: "Rate (95% CI)",
        align: "right",
        render: (p) => (
          <>
            <Pct value={p.conversionRate} />{" "}
            <span className="text-xs text-muted">
              [<Pct value={p.conversionRateLo} unit={false} />–
              <Pct value={p.conversionRateHi} />]
            </span>
          </>
        ),
      },
      {
        key: "cost",
        header: "Cost",
        align: "right",
        // CTO-429. Was an unguarded <Money micro={p.costMicroUsd} />: the aggregate never arrives
        // null, so Money's blank could not fire and an all-unpriced system printed "$0.00".
        render: (p) => {
          const cov = rowCostCoverage(p);
          return <Money micro={cov.micro} reason={cov.reason} />;
        },
      },
      {
        key: "costPerConversion",
        header: `$/${outcome}`,
        align: "right",
        cellClassName: "font-semibold",
        render: (p) => (
          <>
            <Money
              micro={p.costPerConversionMicroUsd}
              reason={
                (p.unpricedSpanCount ?? 0) > 0
                  ? COST_UNKNOWN_FOR_RATIO
                  : `no ${outcome} events for this system in the window, so there is nothing to divide the cost by`
              }
            />
            <span className="sr-only"> per {outcome}</span>
          </>
        ),
      },
      {
        key: "valuePerUser",
        header: "Value/user",
        align: "right",
        render: (p) => (
          <Money
            micro={p.valuePerUserMicroUsd}
            reason={(p.unpricedSpanCount ?? 0) > 0 ? COST_UNKNOWN_FOR_RATIO : NO_REVENUE_WIRED}
          />
        ),
      },
      {
        key: "marginPerUser",
        header: "Margin/user",
        align: "right",
        render: (p) =>
          p.marginPerUserMicroUsd === null ? (
            <Money
              micro={p.marginPerUserMicroUsd}
              reason={
                (p.unpricedSpanCount ?? 0) > 0 ? COST_UNKNOWN_FOR_RATIO : NO_REVENUE_FOR_MARGIN
              }
            />
          ) : (
            <>
              <div
                className={
                  p.marginPerUserMicroUsd >= 0
                    ? "font-semibold text-good"
                    : "font-semibold text-warn"
                }
              >
                <Money micro={p.marginPerUserMicroUsd} />
              </div>
              {p.marginPct !== null && (
                <div className="text-xs text-muted">
                  <Pct value={p.marginPct} />
                </div>
              )}
            </>
          ),
      },
    ],
    [outcome],
  );

  // The chart's stacking order and legend follow the table order (by sessions), so a reader meets
  // the same providers in the same order in both places.
  const chartGroups = useMemo(() => report.perProvider.map((p) => p.provider), [report.perProvider]);
  const chartDays: StackedChartDay[] = useMemo(
    () => (report.dailyByProvider ?? []).map((d) => ({ date: d.date, byGroup: d.byProvider })),
    [report.dailyByProvider],
  );
  const hasChart = chartGroups.length > 0 && chartDays.length > 0;

  // CTO-429: the tiles shared the table's defect. `Total cost` was an unguarded <SummaryTile
  // micro={...}> over the same NULL-skipping sum, so a window in which nothing could be priced
  // announced "$0.00" as the tenant's spend.
  const totalsCov = costCoverage(
    report.totals.costMicroUsd,
    report.totals.spanCount ?? 0,
    report.totals.unpricedSpanCount ?? 0,
    "in this window",
  );
  const totalsUnpriced = (report.totals.unpricedSpanCount ?? 0) > 0;

  const body = (
    <div className="space-y-6">
      <TileGrid>
        <CountTile label="Sessions" value={report.totals.sessions} />
        <CountTile label={`${outcome} events`} value={report.totals.conversions} />
        <SummaryTile
          label="Total cost"
          micro={totalsCov.micro}
          reason={totalsCov.reason || "no cost data for this window"}
        />
        <SummaryTile
          label={`$ / ${outcome}`}
          micro={report.totals.costPerConversionMicroUsd}
          reason={
            totalsUnpriced
              ? "the total cost for this window is unknown, not zero: some spans carry no catalog rate, so anything divided by that cost is unknown too"
              : `no ${outcome} events in the window, so there is nothing to divide the cost by`
          }
        />
      </TileGrid>

      {hasChart ? (
        <Card title="Cost by system">
          <InteractiveStackedChart
            days={chartDays}
            groups={chartGroups}
            ariaLabel="daily cost stacked by system"
            emptyLabel="no spend in this window yet"
          />
        </Card>
      ) : null}

      <Card title={`Per-system · ${outcome}`}>
        {report.perProvider.length === 0 ? (
          // Deliberately not DataTable's `empty` slot. A first-run viewer needs the command that
          // produces data, and a centered line under a header row of empty columns buries it.
          <p className="text-sm text-muted">
            No sessions match these filters yet. Run{" "}
            <code className="rounded bg-ink px-1 py-0.5 text-xs">
              make chatbot-demo
            </code>{" "}
            from <code className="text-xs">infra/</code> to drive synthetic traffic.
          </p>
        ) : (
          <DataTable
            columns={columns}
            rows={report.perProvider}
            rowKey={(p) => p.provider}
            // One row per provider, so a pager would be chrome around a two-row table.
            pageSize={0}
          />
        )}
        {/* #320: say out loud what the rows are. A reader who sees pinecone next to anthropic and
            has not been told the column is `gen_ai.system` will read it as a claim about LLM
            providers. Naming the dimension is the honest fix; filtering the vector rows out would
            hide real spend from the very page that exists to account for it. */}
        <p className="mt-3 text-xs text-muted">
          Rows are <span className="font-mono">gen_ai.system</span>, which is the LLM provider on an
          LLM span and the vector vendor on a vector span, so a vector store can appear here beside
          a model provider. A row showing a cost is real spend for this window; a row whose spans
          carry no catalog rate shows a blank with the reason on hover, never a zero.
        </p>
        <p className="mt-2 text-xs text-muted">
          Intervals are Wilson 95% on the conversion rate: small samples produce
          wide bands, by design. Two systems &ldquo;tie&rdquo; when their bands overlap.
        </p>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Conversions"
        subtitle={
          <>
            $/{outcome} per <span className="font-mono">gen_ai.system</span>, joined from cost spans
            and CDP events on <span className="font-mono">UserIdHash</span>.
          </>
        }
        actions={<LiveIndicator updatedAt={updatedAt} />}
        toolbar={<FilterBar hideGroupBy options={{ provider: PROVIDER_OPTIONS, feature: featureTags.map((f) => ({ value: f })) }} />}
      />
      {/* #364. `state` replaces the old `isMock ? preview : body` pair, which had no way to say
          "the tenant has no sessions yet" other than by showing the fixture's 5,300 of them behind
          a label. Unavailable and empty are now separate answers, and neither renders a figure. */}
      {report.state === "unavailable" ? (
        <SourceUnavailable reason="The telemetry store could not be read for this workspace." />
      ) : report.state === "empty" ? (
        <NoDataYet
          what={`attributed ${outcome} sessions`}
          detail="No sessions have been joined to cost spans for this workspace."
        />
      ) : report.state === "sample" ? (
        <SyntheticPreviewBanner workflow="Attribution">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

/**
 * The cost this row may honestly show (CTO-429). Thin wrapper so the Cost cell and the checks the
 * other cells make read from one place and cannot drift apart.
 */
function rowCostCoverage(p: ProviderAttribution) {
  return costCoverage(p.costMicroUsd, p.spanCount ?? 0, p.unpricedSpanCount ?? 0, ROW_SCOPE);
}

/** A count headline tile matching {@link SummaryTile}'s shape for the non-money metrics. */
function CountTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-edge bg-panel p-4">
      <span className="text-xs font-medium uppercase tracking-wide text-muted">{label}</span>
      <span className="text-2xl font-semibold tabular-nums">{value.toLocaleString()}</span>
    </div>
  );
}
