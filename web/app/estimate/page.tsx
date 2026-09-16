// SPDX-License-Identifier: Apache-2.0
import {
  NoDataYet,
  PartialDataBanner,
  SourceUnavailable,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { Blank } from "@/components/HonestValue";
import { apiGet } from "@/lib/api";
import { asOfLabel, boundaryFromMinutesAgo, deriveDataState, relativeAge } from "@/lib/dataState";
import { type Projection } from "@/lib/estimate";
import { EstimateWhatIf } from "./EstimateWhatIf";

export default async function EstimatePage() {
  const projection = await apiGet<Projection>("/api/estimate");
  const { workload, pr, sample, synthetic, workspaceTraffic } = projection;

  // This projection samples a reconciled historical window; surface that window's freshness so a
  // forecast off a stale baseline is never shown as fresh (CTO-80).
  const reconciledThrough = boundaryFromMinutesAgo(projection.reconcilerLastRunMinutesAgo);
  // CTO-298 follow-up: the empty state keys on the route's first-event probe, not on the baseline.
  //
  // It was `current.monthlyCostMicroUsd === null`, and `current` is filled in by the fixture alone,
  // so that test held for every real tenant: a pilot with a replayed corpus opened this page and
  // was told, as a measured fact, that nothing had reached ai-tally. Worse, the what-if form, the
  // tiles, the driver breakdown and every honest blank below live inside `body`, which a real
  // tenant then never saw. `waiting` is the probe having run and found no span, which is exactly
  // what the copy under it claims.
  const noTraffic = workspaceTraffic === "waiting";
  const thinSample = (sample.used ?? 0) > 0 && sample.pathologicalIncluded === 0;
  const state = deriveDataState({
    isEmpty: noTraffic,
    isPartial: thinSample,
    reconciledThrough,
  });
  const asOf = asOfLabel(reconciledThrough);

  const body = (
    <div className="space-y-6">
      {pr && (
        <div className="rounded-xl border border-edge bg-panel p-4 text-sm">
          <span className="text-muted">Estimating PR </span>
          <span className="font-mono text-accent">
            {pr.repo}#{pr.number}
          </span>
          <span className="text-muted">: </span>
          <span>{pr.title}</span>
        </div>
      )}

      <EstimateWhatIf initial={projection} />
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Estimate</h1>
          <p className="mt-1 text-sm text-muted">
            Workload:{" "}
            {workload === null ? (
              <Blank reason="no replayed workload for this workspace yet" />
            ) : (
              <span className="font-mono text-muted">{workload}</span>
            )}
          </p>
        </div>
        {state !== "empty" && asOf && (
          <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
        )}
      </div>

      {state === "partial" && <PartialDataBanner missing="tail-weighted sampling" />}

      {/*
        CTO-298: the banner keys on the payload's own provenance. It used to key on a zero baseline,
        which the fixture could never produce, so the one payload it existed to label never wore it.
        A real tenant with no baseline gets the new-workspace state instead of a preview of somebody
        else's research_agent.
      */}
      {synthetic ? (
        <SyntheticPreviewBanner workflow="Estimate">{body}</SyntheticPreviewBanner>
      ) : workspaceTraffic === "unknown" ? (
        // The probe could not run, so whether anything has arrived is genuinely unknown. Answering
        // that with "nothing has arrived" would be the same fabrication in a friendlier voice.
        <SourceUnavailable reason="The telemetry store could not be read, so we cannot tell whether any traffic has arrived for this workspace." />
      ) : state === "empty" ? (
        <NoDataYet
          what="traffic for this workspace"
          detail="An estimate is a projection off a measured baseline, and the first-event probe found no spans for this workspace."
        />
      ) : (
        body
      )}
    </div>
  );
}
