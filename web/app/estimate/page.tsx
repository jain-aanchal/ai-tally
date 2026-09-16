// SPDX-License-Identifier: Apache-2.0
import {
  NoDataYet,
  PartialDataBanner,
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
  const { workload, pr, current, sample, synthetic } = projection;

  // This projection samples a reconciled historical window; surface that window's freshness so a
  // forecast off a stale baseline is never shown as fresh (CTO-80).
  const reconciledThrough = boundaryFromMinutesAgo(projection.reconcilerLastRunMinutesAgo);
  // CTO-298: an absent baseline is null, not 0. The old `=== 0` test was never true for the fixture
  // (whose baseline is $19,100/mo), so the empty state it gated was unreachable.
  const noBaseline = current.monthlyCostMicroUsd === null;
  const thinSample = sample.used > 0 && sample.pathologicalIncluded === 0;
  const state = deriveDataState({
    isEmpty: noBaseline,
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
      ) : state === "empty" ? (
        <NoDataYet
          what="baseline cost for this workload"
          detail="An estimate is a projection off a measured baseline, and no priced traffic has reached ai-tally for this workspace yet."
        />
      ) : (
        body
      )}
    </div>
  );
}
