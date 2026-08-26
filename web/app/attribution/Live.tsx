// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for /attribution (CTO-108).
//
// Migrated onto the shared primitives in CTO-179: the per-provider table is a `DataTable` column
// spec, and every number goes through `<Money>` / `<Pct>` so a blank cell carries the reason it is
// blank. Value/user and margin/user are the blanks that matter here. They are empty because no
// revenue source is wired for the tenant, not because those providers earn nothing, and until now
// the page rendered a bare glyph that read as a bug.

"use client";

import { useMemo, type ReactNode } from "react";

import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { DataTable, type Column } from "@/components/DataTable";
import { Money, Pct } from "@/components/HonestValue";
import { LiveIndicator } from "@/components/LiveIndicator";
import type { AttributionReport, ProviderAttribution } from "@/lib/attribution";
import { useLivePoll } from "@/lib/useLivePoll";

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

export function AttributionLive({
  endpoint,
  initialData,
  outcome,
  tag,
  provider,
}: {
  endpoint: string;
  initialData: AttributionReport;
  outcome: string;
  tag: string | null;
  provider: string | null;
}) {
  const { data: report, updatedAt } = useLivePoll<AttributionReport>(endpoint, initialData);

  // Columns close over `outcome`, which comes from the URL filters, so they are rebuilt only when
  // the filter changes rather than on every poll tick.
  const columns = useMemo<Column<ProviderAttribution>[]>(
    () => [
      {
        key: "provider",
        header: "Provider",
        cellClassName: "font-mono",
        render: (p) => p.provider,
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
        header: "LLM cost",
        align: "right",
        render: (p) => <Money micro={p.costMicroUsd} />,
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
              reason={`no ${outcome} events for this provider in the window, so there is nothing to divide the cost by`}
            />
            <span className="sr-only"> per {outcome}</span>
          </>
        ),
      },
      {
        key: "valuePerUser",
        header: "Value/user",
        align: "right",
        render: (p) => <Money micro={p.valuePerUserMicroUsd} reason={NO_REVENUE_WIRED} />,
      },
      {
        key: "marginPerUser",
        header: "Margin/user",
        align: "right",
        render: (p) =>
          p.marginPerUserMicroUsd === null ? (
            <Money micro={p.marginPerUserMicroUsd} reason={NO_REVENUE_FOR_MARGIN} />
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

  const body = (
    <div className="space-y-6">
      <Card title="Headline">
        <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <Headline k="sessions" v={report.totals.sessions.toLocaleString()} />
          <Headline
            k={`${outcome} events`}
            v={report.totals.conversions.toLocaleString()}
          />
          <Headline k="LLM cost" v={<Money micro={report.totals.costMicroUsd} />} />
          <Headline
            k={`$ / ${outcome}`}
            v={
              <Money
                micro={report.totals.costPerConversionMicroUsd}
                reason={`no ${outcome} events in the window, so there is nothing to divide the cost by`}
              />
            }
            highlight
          />
        </dl>
      </Card>

      <Card title={`Per-provider · ${outcome}`}>
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
        <p className="mt-3 text-xs text-muted">
          Intervals are Wilson 95% on the conversion rate — small samples produce
          wide bands, by design. Two providers &ldquo;tie&rdquo; when their bands overlap.
        </p>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Attribution</h1>
          <p className="mt-1 text-sm text-muted">
            $/{outcome} per provider, joined from LLM spans and CDP events on{" "}
            <span className="font-mono">UserIdHash</span>.
          </p>
        </div>
        <LiveIndicator updatedAt={updatedAt} />
      </div>
      {report.isMock ? (
        <SyntheticPreviewBanner workflow="Attribution">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function Headline({
  k,
  v,
  highlight,
}: {
  k: string;
  // ReactNode rather than string: the money headlines are <Money> now, so a missing one carries
  // its own explanation instead of collapsing to a bare glyph on the way in.
  v: ReactNode;
  highlight?: boolean;
}) {
  return (
    <div>
      <dt className="text-xs uppercase text-muted">{k}</dt>
      <dd
        className={`mt-0.5 tabular-nums ${highlight ? "text-lg font-semibold text-good" : "text-base"}`}
      >
        {v}
      </dd>
    </div>
  );
}
