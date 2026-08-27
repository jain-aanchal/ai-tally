// SPDX-License-Identifier: Apache-2.0
// Client-side live wrapper for the home dashboard (CTO-108, restyled onto the D1 design foundation
// in CTO-222/D2).
//
// The visual pass keeps every figure and every honest-blank rule the shipped page had: the headline
// numbers (spend, reconciled, hidden cost) move into SummaryTiles WITHOUT changing what they read,
// the ROI / per-provider cards are untouched, and the data-state banners, LiveIndicator and
// StaleBadge all stay exactly where they were. The standalone "Estimated" tile was dropped (CTO-228):
// it duplicated Spend whenever nothing had reconciled, so the estimated/reconciled split moved into
// the Reconciled tile's coverage hint instead of a second identical number.
//
// FilterBar sits under the title via PageHeader. The time range now re-parameterises the endpoint
// (CTO-226): the managed query string rides on /api/home, so 7d/30d/90d re-query SPEND and the ROI
// snapshot over the chosen window rather than only restyling a fixed roll-up. The dimension
// multi-selects still filter the rows the page already holds rather than re-querying: selecting a
// feature narrows the ROI snapshot, selecting a provider narrows the per-provider conversion table.
// That is an honest hide of rows, never a fabricated slice. Group-by is hidden here because Home has
// no grouped chart to re-key (see the interactive chart on /cost).
//
// CTO-227 replaces the "Top cost outliers" card with a compact month-end forecast: the same
// tenant-wide projection /cost draws (fetched once, not polled), so Home and Cost can never disagree.
// The full burn-down, the confidence cone and the per-scope roster stay on /cost; this is the
// headline plus the one honest caveat, and it links through for the rest. The month-end figure is
// NOT windowed by the range selector: the forecast is always for the current calendar month, which
// the card says out loud so a 7-day range next to a monthly projection cannot read as a mismatch.

"use client";

import Link from "next/link";

import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { Blank, Money, Pct } from "@/components/HonestValue";
import { LiveIndicator } from "@/components/LiveIndicator";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile } from "@/components/SummaryTile";
import type { ProviderAttribution } from "@/lib/attribution";
import type { BurndownSection, ForecastPayload } from "@/lib/burndown";
import { LAYERS, type Layer } from "@/lib/cost";
import { allZero, asOfLabel, deriveDataState, relativeAge, zeroEnabledLayers } from "@/lib/dataState";
import { rangeDays } from "@/lib/filters";
import type { FeatureRoi, SpendSummary } from "@/lib/types";
import { formatUSD } from "@/lib/types";
import { useFilters } from "@/lib/useFilters";
import { useLivePoll } from "@/lib/useLivePoll";

export interface HomePayload {
  spend: SpendSummary;
  roi: FeatureRoi[];
  perProviderConversion: ProviderAttribution[];
}

export function HomeLive({
  initialData,
  enabledLayers,
  forecast,
}: {
  initialData: HomePayload;
  enabledLayers: readonly Layer[];
  /** Tenant-wide month-end projection, the same one /cost draws. Fetched once, not polled. */
  forecast: ForecastPayload;
}) {
  // Same URL-synced filter state the FilterBar writes. Home reads it BOTH to narrow the tables it
  // holds and, since CTO-226, to drive the time-range window: the endpoint carries the managed query
  // string, so flipping 7d/30d/90d changes the endpoint and useLivePoll re-fetches the new window.
  const { state: filterState, queryString } = useFilters();
  const windowDays = rangeDays(filterState.range);
  const endpoint = queryString ? `/api/home?${queryString}` : "/api/home";
  const { data, updatedAt } = useLivePoll<HomePayload>(endpoint, initialData);
  const { spend: s, roi, perProviderConversion } = data;

  const featureFilter = filterState.filters.feature;
  const providerFilter = filterState.filters.provider;

  const featureOptions: FilterOption[] = roi.map((r) => ({ value: r.feature }));
  const providerOptions: FilterOption[] = perProviderConversion.map((p) => ({ value: p.provider }));

  const visibleRoi =
    featureFilter.length > 0 ? roi.filter((r) => featureFilter.includes(r.feature)) : roi;
  const visibleProviders =
    providerFilter.length > 0
      ? perProviderConversion.filter((p) => providerFilter.includes(p.provider))
      : perProviderConversion;

  // Hidden cost is every non-LLM layer: the "all-in" story the tile makes explicit. The percentage
  // and its zero-when-empty behaviour are carried over verbatim (CTO-222 keeps the meaning as-is).
  const hidden =
    s.byLayer.vector + s.byLayer.tools + s.byLayer.compute + s.byLayer.embeddings + s.byLayer.egress;
  const hiddenPct = s.totalMicroUsd === 0 ? 0 : Math.round((hidden / s.totalMicroUsd) * 100);

  const layers: Record<string, number> = { ...s.byLayer };
  const layerTotals = LAYERS.reduce<Record<Layer, number>>(
    (acc, l) => {
      acc[l] = s.byLayer[l];
      return acc;
    },
    { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0 },
  );
  const trippedLayers = zeroEnabledLayers(layerTotals, enabledLayers);
  const state = deriveDataState({
    isEmpty: s.totalMicroUsd === 0 && allZero(layers),
    isPartial: trippedLayers.length > 0,
    reconciledThrough: s.reconciledThrough,
  });
  const asOf = asOfLabel(s.reconciledThrough);
  const hasReconciledDate = s.reconciledThrough > "1970-01-01";

  // Spend is the total; estimated + reconciled sum to it. Showing "Estimated" as its own tile
  // duplicated the Spend headline whenever nothing had reconciled yet (the common state), so the
  // estimated/reconciled split now lives in the Reconciled tile's coverage hint instead of a second
  // identical number (CTO-228). "Estimated" is always Spend minus Reconciled, so no figure is lost.
  const reconciledPct =
    s.totalMicroUsd === 0 ? 0 : Math.round((s.reconciledMicroUsd / s.totalMicroUsd) * 100);
  const tiles = (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <SummaryTile
        label="Spend"
        micro={s.totalMicroUsd}
        hint={`last ${windowDays} days`}
        higherIsBetter={false}
      />
      <SummaryTile
        label="Reconciled"
        micro={s.reconciledMicroUsd}
        hint={
          hasReconciledDate
            ? `${reconciledPct}% invoice-confirmed, through ${s.reconciledThrough}`
            : "not yet invoice-confirmed (all still estimated)"
        }
      />
      <SummaryTile
        label="Hidden cost"
        micro={hidden}
        hint={`${hiddenPct}% of spend · vector + tools + compute`}
      />
    </div>
  );

  const grid = (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <MonthlyForecastCard forecast={forecast} />

      <Card title="ROI snapshot">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">Feature</th>
              <th className="py-1 text-right font-medium">Cost/user</th>
              <th className="py-1 text-right font-medium">Value/user</th>
              <th className="py-1 text-right font-medium">Payback</th>
            </tr>
          </thead>
          <tbody>
            {visibleRoi.map((r) => (
              <tr key={r.feature} className="border-t border-edge">
                <td className="py-1.5">{r.feature}</td>
                <td className="py-1.5 text-right">{formatUSD(r.costPerUserMicroUsd)}</td>
                <td className="py-1.5 text-right">
                  {r.valuePerUserMicroUsd === null ? (
                    <span className="text-muted">—</span>
                  ) : (
                    formatUSD(r.valuePerUserMicroUsd)
                  )}
                </td>
                <td className="py-1.5 text-right">
                  {r.paybackDays === null ? (
                    <span className="text-muted">unattributed</span>
                  ) : (
                    `${r.paybackDays}d`
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Per-provider · conversion">
        {perProviderConversion.length === 0 ? (
          <p className="text-sm text-muted">
            No sessions yet — drive traffic to populate (link out from{" "}
            <a className="text-good underline" href="/attribution">
              Attribution
            </a>
            ).
          </p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Provider</th>
                <th className="py-1 text-right font-medium">Sessions</th>
                <th className="py-1 text-right font-medium">Conversions</th>
                <th className="py-1 text-right font-medium">Rate</th>
                <th className="py-1 text-right font-medium">$/conversion</th>
              </tr>
            </thead>
            <tbody>
              {visibleProviders.map((p) => (
                <tr key={p.provider} className="border-t border-edge">
                  <td className="py-1.5 font-mono">{p.provider}</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {p.sessions.toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {p.conversions.toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right tabular-nums">
                    {(p.conversionRate * 100).toFixed(1)}%
                  </td>
                  <td className="py-1.5 text-right font-semibold tabular-nums">
                    {p.costPerConversionMicroUsd === null
                      ? "—"
                      : formatUSD(p.costPerConversionMicroUsd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );

  const body = (
    <div className="space-y-6">
      {tiles}
      {grid}
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Home"
        subtitle="What your AI costs, and whether it pays for itself"
        actions={
          <>
            <LiveIndicator updatedAt={updatedAt} />
            {state !== "empty" && asOf && (
              <StaleBadge
                asOf={asOf}
                age={relativeAge(s.reconciledThrough)}
                stale={state === "stale"}
              />
            )}
          </>
        }
        toolbar={
          <FilterBar
            options={{ feature: featureOptions, provider: providerOptions }}
            hideGroupBy
          />
        }
      />

      {state === "partial" && <PartialDataBanner trippedLayers={trippedLayers} />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Home">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

/**
 * The compact month-end forecast that replaced the cost-outliers card (CTO-227). It renders the
 * headline and the ONE caveat that decides whether to believe it, then links to /cost for the cone,
 * the input window and the per-scope roster. It reuses the exact section /cost draws, so the two
 * surfaces can never state different projections. Every honest-blank rule the burn-down card
 * enforces is preserved: a refusal below the history floor is a blank with its reason, never a
 * flattering zero; a missing budget shows the projection with no variance rather than a variance
 * against zero.
 */
function MonthlyForecastCard({ forecast }: { forecast: ForecastPayload }) {
  const section = forecast.section;
  if (!section) {
    return (
      <Card title="Monthly predicted AI cost">
        <p className="text-sm text-muted">
          <Blank reason={forecast.unavailable ?? "this forecast could not be computed"} /> No
          forecast: {forecast.unavailable ?? "this forecast could not be computed"}.
        </p>
      </Card>
    );
  }

  const { forecast: f, period } = section;
  const month = period.start.slice(0, 7);

  if (f.status === "insufficient_history") {
    // The refusal, compact. Not a claim of safety: "we do not know" is a different answer from
    // "this will not breach", and the full explanation lives on /cost.
    const short = Math.max(0, f.historyDays);
    return (
      <Card title="Monthly predicted AI cost">
        <div className="text-2xl font-semibold text-muted">
          <Blank reason={`only ${short} settled days of history, 14 are needed to project`} /> Not
          enough history yet
        </div>
        <p className="mt-2 text-sm text-muted">
          A projection needs at least two full weeks of settled days so each weekday has a real
          median. This is not a statement that spend is under control, only that we will not put a
          volatile number on screen.{" "}
          <Link href="/cost" className="text-accent underline">
            See the forecast detail
          </Link>
          .
        </p>
      </Card>
    );
  }

  return (
    <Card title="Monthly predicted AI cost">
      <div className="text-3xl font-semibold tabular-nums">
        <Money micro={f.projectedMicroUsd} reason="nothing was projected" />
      </div>
      <div className="mt-1 text-sm text-muted">
        all-in AI spend for {month} (LLM, vector, tools, compute, embeddings, egress), a forecast and
        not a commitment
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted">80% range</dt>
          <dd className="mt-0.5 tabular-nums">
            <Money micro={f.lowMicroUsd} reason="nothing was projected" /> to{" "}
            <Money micro={f.highMicroUsd} reason="nothing was projected" />
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted">Settled so far</dt>
          <dd className="mt-0.5 tabular-nums">
            <Money micro={f.spendToDateMicroUsd} />
            <span className="ml-2 text-xs text-muted">
              {f.daysElapsed} of {f.daysInPeriod} days
            </span>
          </dd>
        </div>
      </dl>

      <div className="mt-4 border-t border-edge pt-3 text-sm">
        <ForecastStanding section={section} />
      </div>
    </Card>
  );
}

/** The one budget line: the breach date, the under-budget note, or the honest "no budget" state. */
function ForecastStanding({ section }: { section: BurndownSection }) {
  const { breach } = section.forecast;
  const { budget, varianceMicroUsd, variancePct, period } = section;

  if (breach.outcome === "no_budget") {
    return (
      <p className="text-muted">
        <Blank reason={section.noBudgetReason ?? "no budget set"} /> No monthly budget is set, so
        there is no breach date or variance.{" "}
        <Link href="/settings/budgets" className="text-accent underline">
          Set a budget
        </Link>{" "}
        to track this against one.
      </p>
    );
  }

  if (breach.outcome === "breaches" && breach.date !== null) {
    return (
      <p>
        <span className="font-medium text-bad">Crosses budget {breach.date}</span>{" "}
        <span className="text-bad">
          ({varianceMicroUsd !== null && varianceMicroUsd > 0 ? "+" : ""}
          <Money micro={varianceMicroUsd} reason="no budget to compare against" /> over
          {variancePct === null ? null : (
            <>
              {" "}
              <Pct value={variancePct} />
            </>
          )}
          )
        </span>{" "}
        by {period.end} against the{" "}
        <Money micro={budget?.amountMicroUsd ?? null} reason="no budget set" /> budget.
      </p>
    );
  }

  // `never`: projected, and it stays under. Said in those words, distinct from "cannot project".
  return (
    <p>
      <span className="font-medium text-good">Under budget this month</span>{" "}
      <span className="text-good">
        (
        <Money
          micro={varianceMicroUsd === null ? null : Math.abs(varianceMicroUsd)}
          reason="no budget to compare against"
        />{" "}
        under
        {variancePct === null ? null : (
          <>
            {" "}
            <Pct value={Math.abs(variancePct)} />
          </>
        )}
        )
      </span>{" "}
      versus the <Money micro={budget?.amountMicroUsd ?? null} reason="no budget set" /> budget by{" "}
      {period.end}.
    </p>
  );
}
