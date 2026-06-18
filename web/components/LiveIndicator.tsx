// SPDX-License-Identifier: Apache-2.0
// Compact "live · updated Ns ago" badge for dashboard pages (CTO-108).
//
// Sits alongside the existing StaleBadge in the page header — additive, not replacing it.
// StaleBadge reports the reconciler-boundary freshness; LiveIndicator reports whether the page
// itself is auto-refreshing and how long since the last successful client fetch.

"use client";

import { useEffect, useState } from "react";

import { relativeAge } from "@/lib/dataState";

export function LiveIndicator({
  updatedAt,
  paused,
}: {
  updatedAt: Date | null;
  paused?: boolean;
}) {
  // Re-tick once per second so the "Ns ago" label stays fresh even between polls.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const age = updatedAt ? relativeAge(updatedAt.toISOString()) : null;
  const dotClass = paused ? "bg-muted" : "animate-pulse bg-good";
  const label = paused
    ? "Paused"
    : age
      ? `Live · updated ${age}`
      : "Live";

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-ink px-2.5 py-1 text-xs text-muted"
      title={updatedAt ? `Last update ${updatedAt.toLocaleTimeString()}` : "Waiting for first update"}
    >
      <span aria-hidden className={`inline-block h-1.5 w-1.5 rounded-full ${dotClass}`} />
      {label}
    </span>
  );
}
