// SPDX-License-Identifier: Apache-2.0
// Client-side polling hook for dashboard live updates (CTO-108).
//
// Every dashboard page is a Server Component that queries once on render. To make new data appear
// without a manual reload, we wrap the rendered block in a small client component that takes the
// SSR-rendered payload as `initialData`, then re-fetches the same JSON endpoint on an interval.
//
// Design notes:
//   - SSR stays the source of truth for the first paint; the first client fetch is delayed by
//     intervalMs, so there's no double-render storm on mount.
//   - Tab-visibility-aware: when the tab is hidden we pause the interval and cancel any in-flight
//     request. On focus we fetch once immediately and resume the interval.
//   - On error, we KEEP the last good data — never let a transient 5xx blank a page. The error
//     is surfaced via the returned `error` field for callers that want to badge it.
//   - AbortController per request; cancels in-flight on unmount or visibility change.
//   - Cadence is read from NEXT_PUBLIC_TALLY_DASHBOARD_REFRESH_MS (default 5000). `0` disables.

"use client";

import { useEffect, useRef, useState } from "react";

function defaultIntervalMs(): number {
  const raw = process.env.NEXT_PUBLIC_TALLY_DASHBOARD_REFRESH_MS;
  if (raw === undefined || raw === "") return 5000;
  const n = Number.parseInt(raw, 10);
  if (Number.isNaN(n) || n < 0) return 5000;
  return n;
}

export interface UseLivePollResult<T> {
  data: T;
  updatedAt: Date | null;
  error: Error | null;
}

export interface UseLivePollOptions {
  intervalMs?: number;
  enabled?: boolean;
}

/**
 * Periodically re-fetch from a JSON endpoint, with tab-visibility-aware pause/resume.
 * Returns the latest data (or the initial server-rendered data while waiting), an `updatedAt` Date,
 * and an `error: Error | null` for the last failed attempt (data stays frozen on error).
 *
 * The signature mirrors how the existing apiGet result is consumed so swapping a page over is a
 * one-line change at the call site.
 */
export function useLivePoll<T>(
  endpoint: string,
  initialData: T,
  options?: UseLivePollOptions,
): UseLivePollResult<T> {
  const envInterval = defaultIntervalMs();
  const intervalMs = options?.intervalMs ?? envInterval;
  const enabled = (options?.enabled ?? true) && intervalMs > 0;

  const [data, setData] = useState<T>(initialData);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [error, setError] = useState<Error | null>(null);

  // Refs keep the polling loop stable across renders without re-subscribing.
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;

    async function fetchOnce() {
      // Cancel any in-flight fetch before starting a new one.
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        const res = await fetch(endpoint, { signal: ctrl.signal, cache: "no-store" });
        if (!res.ok) throw new Error(`live-poll ${endpoint} failed: ${res.status}`);
        const json = (await res.json()) as T;
        if (cancelled) return;
        setData(json);
        setUpdatedAt(new Date());
        setError(null);
      } catch (err) {
        // Aborts are expected; don't surface as errors.
        if (err instanceof Error && err.name === "AbortError") return;
        if (cancelled) return;
        const e = err instanceof Error ? err : new Error(String(err));
        // eslint-disable-next-line no-console
        console.warn("[live-poll]", e);
        setError(e);
      }
    }

    function startInterval() {
      if (timerRef.current !== null) return;
      timerRef.current = setInterval(() => {
        void fetchOnce();
      }, intervalMs);
    }

    function stopInterval() {
      if (timerRef.current !== null) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }

    function onVisibilityChange() {
      if (typeof document === "undefined") return;
      if (document.visibilityState === "hidden") {
        stopInterval();
        abortRef.current?.abort();
      } else {
        void fetchOnce();
        startInterval();
      }
    }

    // Start in whatever state the tab is currently in.
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      // Paused initially; wait for visibilitychange to resume.
    } else {
      startInterval();
    }

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    return () => {
      cancelled = true;
      stopInterval();
      abortRef.current?.abort();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    };
  }, [endpoint, intervalMs, enabled]);

  return { data, updatedAt, error };
}
