// SPDX-License-Identifier: Apache-2.0
// Workflow 4 — business-outcome attribution.
//
// Joins LLM cost spans (from otel_spans) with CDP outcome events (from
// business_events) by UserIdHash, filtered by feature tag / provider /
// outcome type. The dashboard's headline number is $/conversion per provider
// with a Wilson confidence band, so the user can see when "Anthropic is
// cheaper per conversion" is statistically meaningful vs. just small-sample
// noise.
//
// Today only the chatbot demo emits chatbot.real_provider on spans and
// conversion / positive_feedback events; other tenants can opt in by
// emitting the same shape.

import type { MicroUSD } from "./types";

export interface AttributionFilters {
  tag: string | null;
  provider: "openai" | "anthropic" | null;
  outcome: "conversion" | "positive_feedback" | "session_engaged" | null;
}

export interface ProviderAttribution {
  provider: string; // "openai" | "anthropic" | "unknown"
  sessions: number;
  conversions: number;
  costMicroUsd: MicroUSD;
  // Headline: $/conversion. NaN when conversions === 0 (the page renders "—").
  costPerConversionMicroUsd: MicroUSD | null;
  // 95% Wilson interval on conversion rate. lo/hi are absolute rates (0..1).
  conversionRate: number;
  conversionRateLo: number;
  conversionRateHi: number;
}

export interface AttributionReport {
  filters: AttributionFilters;
  perProvider: ProviderAttribution[];
  totals: {
    sessions: number;
    conversions: number;
    costMicroUsd: MicroUSD;
    costPerConversionMicroUsd: MicroUSD | null;
  };
  // True when ClickHouse couldn't be reached — the page falls back to mock.
  isMock: boolean;
}

/**
 * Wilson score interval at z=1.96 (95%). More honest than normal-approx for
 * small samples (which the demo will always have). Returns [lo, hi] in
 * absolute conversion-rate units.
 */
export function wilsonInterval(
  successes: number,
  trials: number,
  z = 1.96,
): { lo: number; hi: number; p: number } {
  if (trials <= 0) return { lo: 0, hi: 0, p: 0 };
  const p = successes / trials;
  const denom = 1 + (z * z) / trials;
  const center = (p + (z * z) / (2 * trials)) / denom;
  const half =
    (z * Math.sqrt((p * (1 - p)) / trials + (z * z) / (4 * trials * trials))) /
    denom;
  return {
    p,
    lo: Math.max(0, center - half),
    hi: Math.min(1, center + half),
  };
}

/**
 * Build a ProviderAttribution row from the raw join. `costMicroUsd` and
 * `conversions` are independently aggregated upstream (cost is per-session
 * sum, conversions are distinct events); this only does the arithmetic.
 */
export function buildProviderRow(
  provider: string,
  sessions: number,
  conversions: number,
  costMicroUsd: MicroUSD,
): ProviderAttribution {
  const { p, lo, hi } = wilsonInterval(conversions, sessions);
  const costPerConversion =
    conversions > 0 ? Math.round(costMicroUsd / conversions) : null;
  return {
    provider,
    sessions,
    conversions,
    costMicroUsd,
    costPerConversionMicroUsd: costPerConversion,
    conversionRate: p,
    conversionRateLo: lo,
    conversionRateHi: hi,
  };
}

/** Empty report when nothing has been ingested yet. */
export function emptyReport(filters: AttributionFilters): AttributionReport {
  return {
    filters,
    perProvider: [],
    totals: { sessions: 0, conversions: 0, costMicroUsd: 0, costPerConversionMicroUsd: null },
    isMock: false,
  };
}

/**
 * Mock report used in CI / fresh-clone where the gateway isn't running. The
 * shape mirrors a real two-provider demo so the page renders something
 * sensible to the eye, with isMock=true so the UI can flag it.
 */
export function mockReport(filters: AttributionFilters): AttributionReport {
  const perProvider = [
    buildProviderRow("openai", 25, 5, 850_000),
    buildProviderRow("anthropic", 25, 7, 720_000),
  ];
  const totals = {
    sessions: perProvider.reduce((s, p) => s + p.sessions, 0),
    conversions: perProvider.reduce((s, p) => s + p.conversions, 0),
    costMicroUsd: perProvider.reduce((s, p) => s + p.costMicroUsd, 0),
    costPerConversionMicroUsd: 0,
  };
  totals.costPerConversionMicroUsd =
    totals.conversions > 0 ? Math.round(totals.costMicroUsd / totals.conversions) : null;
  return { filters, perProvider, totals, isMock: true };
}

/** Parse URL search params into typed filters. */
export function parseFilters(searchParams: URLSearchParams): AttributionFilters {
  const tag = searchParams.get("tag");
  const providerRaw = searchParams.get("provider");
  const outcomeRaw = searchParams.get("outcome");
  const provider =
    providerRaw === "openai" || providerRaw === "anthropic" ? providerRaw : null;
  const outcome =
    outcomeRaw === "conversion" ||
    outcomeRaw === "positive_feedback" ||
    outcomeRaw === "session_engaged"
      ? outcomeRaw
      : null;
  return { tag: tag || null, provider, outcome };
}
