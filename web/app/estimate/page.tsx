// SPDX-License-Identifier: Apache-2.0
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import { asOfLabel, boundaryFromMinutesAgo, deriveDataState, relativeAge } from "@/lib/dataState";
import { type Projection } from "@/lib/estimate";
import { EstimateWhatIf } from "./EstimateWhatIf";

export default async function EstimatePage() {
  const projection = await apiGet<Projection>("/api/estimate");
  const { workload, pr, current, sample } = projection;

  // This projection samples a reconciled historical window — surface that window's freshness so a
  // forecast off a stale baseline is never shown as fresh (CTO-80).
  const reconciledThrough = boundaryFromMinutesAgo(projection.reconcilerLastRunMinutesAgo);
  const noBaseline = current.monthlyCostMicroUsd === 0;
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
          <span className="text-muted"> — </span>
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
            Workload: <span className="font-mono text-gray-300">{workload}</span>
          </p>
        </div>
        {state !== "empty" && asOf && (
          <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
        )}
      </div>

      {state === "partial" && <PartialDataBanner missing="tail-weighted sampling" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Estimate">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}
