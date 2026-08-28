// SPDX-License-Identifier: Apache-2.0
// The single-feature detail block inside the unified Cost explorer (CTO-242, M3 of CTO-239).
//
// When /cost groups by `feature` AND is filtered to exactly one feature, this renders that feature's
// economics below the breakdown table, preserving everything the retired /features view showed for
// one feature: the per-feature unit economics (cost/user, value/user, payback, margin, attribution
// rate) with the inline value-event configuration, and the per-feature + tenant-wide attribution
// diagnostics. Honest-under-uncertainty throughout: a source we cannot reach and an unknown value
// each state why, never a fabricated or zero figure.
//
// It reuses the SAME source and shapes as /features rather than reimplementing: a fetch of
// /api/features (queryFeatureEconomics + queryAttributionDiagnostics), the FeatureEconomics /
// AttributionDiagnostics types from lib/features, and the FeatureValueEvents component, which carries
// the unit-economics table AND the value-event POST/config path intact. /api/features is tenant-wide
// and reads no window, so this detail takes no query string (unlike AgentDetail, whose source honors
// the window). Passing FeatureValueEvents a single-element list yields that one feature's economics
// row plus its value-event CTA, so nothing the Features page showed for a feature is lost.

"use client";

import { useEffect, useState } from "react";

import { Card } from "@/components/Card";
import { StaleBadge } from "@/components/DataStateBanner";
import { Diag } from "@/components/Diag";
import {
  type AttributionDiagnostics,
  type FeatureEconomics,
} from "@/lib/features";
import { asOfLabel, deriveDataState, relativeAge, STALE_AFTER_MS } from "@/lib/dataState";

import { FeatureValueEvents } from "../features/FeatureValueEvents";

interface FeaturesDetailPayload {
  features: FeatureEconomics[];
  diagnostics: AttributionDiagnostics;
}

type FetchStatus = "loading" | "ready" | "unavailable";

/**
 * @param feature the selected feature tag (filters.feature[0]). /api/features is tenant-wide and
 *   reads no window, so unlike AgentDetail this component needs no query string.
 */
export function FeatureDetail({ feature }: { feature: string }) {
  const [data, setData] = useState<FeaturesDetailPayload | null>(null);
  const [status, setStatus] = useState<FetchStatus>("loading");

  useEffect(() => {
    const ctrl = new AbortController();
    setStatus("loading");
    fetch("/api/features", { signal: ctrl.signal, cache: "no-store" })
      .then((r) => r.json())
      .then((j: FeaturesDetailPayload) => {
        setData(j);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        // A fast filter change aborts in-flight; keep the last state rather than flashing a message.
        if (err instanceof Error && err.name === "AbortError") return;
        // Honest-under-uncertainty: a failed fetch is "unavailable", never a zero-filled detail.
        setData(null);
        setStatus("unavailable");
      });
    return () => ctrl.abort();
  }, [feature]);

  const title = `Feature economics — ${feature}`;

  if (status === "loading" && data === null) {
    return (
      <Card title={title}>
        <p className="py-6 text-center text-sm text-muted">Loading this feature’s economics…</p>
      </Card>
    );
  }

  if (status === "unavailable" || data === null) {
    return (
      <Card title={title}>
        <p className="py-6 text-center text-sm text-muted">
          This detail is served live and the telemetry source could not be reached, so no economics
          are drawn.
        </p>
      </Card>
    );
  }

  const selected = data.features.find((f) => f.feature === feature) ?? null;

  // No economics row for the selected feature means it has none in this tenant. State it; never
  // fabricate a zero.
  if (selected === null) {
    return (
      <Card title={title}>
        <p className="py-6 text-center text-sm text-muted">
          No economics for <span className="font-mono">{feature}</span>.
        </p>
      </Card>
    );
  }

  // Features has no reconciliation date; the reconciler's last-run minutes is its freshness signal,
  // rendered exactly as the /features view did.
  const reconciledThrough = new Date(
    Date.now() - data.diagnostics.reconcilerLastRunMinutesAgo * 60_000,
  ).toISOString();
  const asOf = asOfLabel(reconciledThrough);
  const dataState = deriveDataState({ isEmpty: false, isPartial: false, reconciledThrough });
  const reconcilerStale = data.diagnostics.reconcilerLastRunMinutesAgo * 60_000 > STALE_AFTER_MS;

  return (
    <div className="space-y-6">
      {asOf && (
        <div className="flex justify-end">
          <StaleBadge
            asOf={asOf}
            age={relativeAge(reconciledThrough)}
            stale={dataState === "stale"}
          />
        </div>
      )}

      {/* The unit-economics table (cost/user, value/user, payback, margin, attribution rate) AND the
          inline value-event config CTA + POST path, reused verbatim from /features. A single-element
          list yields exactly this one feature's row, so the value/user and payback honest blanks and
          the "configure value event →" flow are preserved unchanged (CTO-242). */}
      <FeatureValueEvents initialFeatures={[selected]} />

      <Card title="Attribution diagnostics">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-xs uppercase text-muted">Confidence breakdown</h3>
            {selected.attributionRate === null ? (
              // Honest-under-uncertainty: below the trust floor queryFeatureEconomics nulls the rate,
              // so there is no confidence split to draw. State it rather than showing an empty bar.
              <p className="text-sm text-muted">
                Not enough attributed conversions to report a confidence breakdown for this feature.
              </p>
            ) : (
              <>
                <div className="mb-1 flex items-baseline justify-between text-sm">
                  <span className="font-medium">{selected.feature}</span>
                  <span className="tabular-nums text-muted">
                    {Math.round((selected.attributionRate ?? 0) * 100)}% attributed
                  </span>
                </div>
                <ConfidenceBar b={selected.attributionBreakdown} />
                <Legend />
              </>
            )}
          </div>

          <div>
            <h3 className="mb-2 text-xs uppercase text-muted">Tenant-wide</h3>
            <dl className="space-y-1.5 text-sm">
              <Diag
                k="late-arriving events (7d)"
                v={data.diagnostics.lateArrivalEvents7d.toLocaleString()}
                row
              />
              <Diag
                k="median lag"
                v={`${data.diagnostics.lateArrivalMedianHours.toFixed(1)}h`}
                row
              />
              <Diag
                k="reconciler last ran"
                v={`${data.diagnostics.reconcilerLastRunMinutesAgo} min ago`}
                good={!reconcilerStale}
                row
              />
            </dl>
          </div>
        </div>
      </Card>
    </div>
  );
}

// The confidence bar / legend / diagnostics row are lifted from the retired /features page verbatim
// (CTO-242) so the per-feature attribution split reads identically to what the Features tab showed.
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
      <div
        style={{ width: seg(b.sessionStitched) }}
        className="bg-accent"
        title={`session: ${b.sessionStitched}`}
      />
      <div
        style={{ width: seg(b.identityGraphStitched) }}
        className="bg-warn"
        title={`identity graph: ${b.identityGraphStitched}`}
      />
      <div
        style={{ width: seg(b.unmatched) }}
        className="bg-bad/70"
        title={`unmatched: ${b.unmatched}`}
      />
    </div>
  );
}

function Legend() {
  const dot = (cls: string) => (
    <span aria-hidden className={`inline-block h-2 w-2 rounded-sm ${cls}`} />
  );
  return (
    <ul className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
      <li className="flex items-center gap-1.5">{dot("bg-good")} direct</li>
      <li className="flex items-center gap-1.5">{dot("bg-accent")} session</li>
      <li className="flex items-center gap-1.5">{dot("bg-warn")} identity graph</li>
      <li className="flex items-center gap-1.5">{dot("bg-bad/70")} unmatched</li>
    </ul>
  );
}
