// SPDX-License-Identifier: Apache-2.0
// Shared empty / partial / stale UI for every workflow (CTO-80, spec 13.8 "never show stale as
// fresh"). One implementation, applied identically across all five surfaces.

import Link from "next/link";
import type { ReactNode } from "react";

/** Single connector call-to-action. Links to /connectors, which already exists. */
export function ConnectorCta({ label = "Connect a data source" }: { label?: string }) {
  return (
    <Link
      href="/connectors"
      className="inline-flex items-center rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25"
    >
      {label}
    </Link>
  );
}

/**
 * Pre-data state. Wraps a synthetic preview of the workflow with an unmistakable "SAMPLE DATA"
 * label and a single connector CTA, so a preview is never confused for real data.
 */
export function SyntheticPreviewBanner({
  workflow,
  children,
}: {
  workflow: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-dashed border-accent/40 bg-accent/5">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-dashed border-accent/30 px-4 py-3">
        <div className="flex items-center gap-2 text-sm">
          <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-accent">
            Sample data
          </span>
          <span className="text-muted">
            Preview of {workflow} — no real telemetry yet. These numbers are synthetic.
          </span>
        </div>
        <ConnectorCta />
      </div>
      <div className="relative p-4">
        <span
          aria-hidden
          className="pointer-events-none absolute right-3 top-3 select-none rounded bg-ink/70 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-accent/80"
        >
          Preview
        </span>
        <div className="opacity-90">{children}</div>
      </div>
    </div>
  );
}

/**
 * Partial-data state. A banner pointing at the missing connector, rendered above whatever real
 * data does exist.
 */
export function PartialDataBanner({ missing }: { missing: string }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-sm">
      <div className="text-warn">
        <span className="font-medium">Partial data. </span>
        <span>Showing what we have, but {missing} isn&apos;t connected yet — some numbers are incomplete.</span>
      </div>
      <ConnectorCta label="Finish setup" />
    </div>
  );
}

/**
 * "Data as of" badge. Fresh: a muted timestamp. Stale (boundary &gt; 2h old): turns into a warning
 * so stale numbers are never presented as fresh.
 */
export function StaleBadge({
  asOf,
  age,
  stale,
}: {
  asOf: string;
  age: string;
  stale: boolean;
}) {
  if (stale) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-warn/50 bg-warn/10 px-2.5 py-1 text-xs font-medium text-warn">
        <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-warn" />
        Stale — reconciled {age} (as of {asOf})
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-ink px-2.5 py-1 text-xs text-muted">
      <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-good" />
      Data as of {asOf}
    </span>
  );
}

/**
 * Empty state for surfaces with no reconciliation boundary (what-if tools): a labelled synthetic
 * preview plus a single connector CTA.
 */
export function EmptyState({
  workflow,
  children,
}: {
  workflow: string;
  children: ReactNode;
}) {
  return <SyntheticPreviewBanner workflow={workflow}>{children}</SyntheticPreviewBanner>;
}
