// SPDX-License-Identifier: Apache-2.0
// Features / business attribution, rebuilt on the interactive design foundation (CTO-224, on
// CTO-221 / CTO-220). Design + interactivity only: the per-feature unit-economics table (with its
// honest blank cells and inline value-event config), the finish-setup banner and the attribution
// diagnostics are preserved verbatim. Added: the FilterBar (time range + group-by feature/model +
// a feature filter) and the interactive cost-over-time-by-feature chart.
import { Suspense } from "react";

import { Card } from "@/components/Card";
import { StaleBadge, SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { ExploreChartCard } from "@/components/ExploreChartCard";
import { FilterBar } from "@/components/FilterBar";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import { deriveDataState, relativeAge, STALE_AFTER_MS } from "@/lib/dataState";
import {
  type AttributionDiagnostics,
  type FeatureEconomics,
} from "@/lib/features";
import { FeatureValueEvents } from "./FeatureValueEvents";

interface FeaturesPayload {
  features: FeatureEconomics[];
  diagnostics: AttributionDiagnostics;
}

export default async function FeaturesPage() {
  const { features, diagnostics } = await apiGet<FeaturesPayload>("/api/features");

  // Features has no reconciliation date; the reconciler's last-run minutes is its freshness signal.
  const reconciledThrough = new Date(
    Date.now() - diagnostics.reconcilerLastRunMinutesAgo * 60_000,
  ).toISOString();
  const noEconomics = features.length === 0 || features.every((f) => f.costPerUserMicroUsd === 0);
  const someUnattributed =
    features.some((f) => f.valueEvent === null) && features.some((f) => f.valueEvent !== null);
  const state = deriveDataState({
    isEmpty: noEconomics,
    isPartial: someUnattributed,
    reconciledThrough,
  });
  const reconcilerStale = diagnostics.reconcilerLastRunMinutesAgo * 60_000 > STALE_AFTER_MS;

  // The "Finish setup" onboarding banner and the per-row "configure value event →" CTA live inside
  // FeatureValueEvents (CTO-140) — a client component so both can open the config modal and clear
  // the banner reactively as each feature gets a value event.
  const featureOptions = features.map((f) => ({ value: f.feature }));

  const body = (
    <div className="space-y-6">
      <ExploreChartCard
        title="Feature cost over time"
        groupByChoices={["feature", "model"]}
        defaultGroupBy="feature"
      />

      <FeatureValueEvents initialFeatures={features} />

      <Card title="Attribution diagnostics">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-xs uppercase text-muted">Per-feature confidence breakdown</h3>
            <ul className="space-y-3 text-sm">
              {features
                .filter((f) => f.attributionRate !== null)
                .map((f) => (
                  <li key={f.feature}>
                    <div className="mb-1 flex items-baseline justify-between">
                      <span className="font-medium">{f.feature}</span>
                      <span className="tabular-nums text-muted">
                        {Math.round((f.attributionRate ?? 0) * 100)}% attributed
                      </span>
                    </div>
                    <ConfidenceBar b={f.attributionBreakdown} />
                  </li>
                ))}
            </ul>
            <Legend />
          </div>

          <div>
            <h3 className="mb-2 text-xs uppercase text-muted">Tenant-wide</h3>
            <dl className="space-y-1.5 text-sm">
              <Diag k="late-arriving events (7d)" v={diagnostics.lateArrivalEvents7d.toLocaleString()} />
              <Diag k="median lag" v={`${diagnostics.lateArrivalMedianHours.toFixed(1)}h`} />
              <Diag
                k="reconciler last ran"
                v={`${diagnostics.reconcilerLastRunMinutesAgo} min ago`}
                good={!reconcilerStale}
              />
            </dl>
          </div>
        </div>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Features"
        subtitle="What each feature costs per user, the value attributed to it, and the ROI margin."
        actions={
          state !== "empty" ? (
            <StaleBadge
              asOf={relativeAge(reconciledThrough)}
              age={relativeAge(reconciledThrough)}
              stale={state === "stale"}
            />
          ) : undefined
        }
        toolbar={
          <Suspense fallback={null}>
            <FilterBar
              groupByChoices={["feature", "model"]}
              defaultGroupBy="feature"
              options={{ feature: featureOptions }}
            />
          </Suspense>
        }
      />

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Features">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function ConfidenceBar({
  b,
}: {
  b: { direct: number; sessionStitched: number; identityGraphStitched: number; unmatched: number };
}) {
  const total = b.direct + b.sessionStitched + b.identityGraphStitched + b.unmatched;
  if (total === 0) return null;
  const seg = (n: number) => `${(n / total) * 100}%`;
  return (
    <div className="flex h-2 w-full overflow-hidden rounded bg-edge">
      <div style={{ width: seg(b.direct) }} className="bg-good" title={`direct: ${b.direct}`} />
      <div style={{ width: seg(b.sessionStitched) }} className="bg-accent" title={`session: ${b.sessionStitched}`} />
      <div
        style={{ width: seg(b.identityGraphStitched) }}
        className="bg-warn"
        title={`identity graph: ${b.identityGraphStitched}`}
      />
      <div style={{ width: seg(b.unmatched) }} className="bg-bad/70" title={`unmatched: ${b.unmatched}`} />
    </div>
  );
}

function Legend() {
  const dot = (cls: string) => <span aria-hidden className={`inline-block h-2 w-2 rounded-sm ${cls}`} />;
  return (
    <ul className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
      <li className="flex items-center gap-1.5">{dot("bg-good")} direct</li>
      <li className="flex items-center gap-1.5">{dot("bg-accent")} session</li>
      <li className="flex items-center gap-1.5">{dot("bg-warn")} identity graph</li>
      <li className="flex items-center gap-1.5">{dot("bg-bad/70")} unmatched</li>
    </ul>
  );
}

function Diag({ k, v, good }: { k: string; v: string; good?: boolean }) {
  return (
    <div className="flex items-baseline justify-between">
      <dt className="text-muted">{k}</dt>
      <dd className={good ? "text-good" : ""}>{v}</dd>
    </div>
  );
}
