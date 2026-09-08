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
// Provider is read from the standard gen_ai.system column (post-CTO-106);
// historical rows that still carry the chatbot.real_provider long-tail
// attribute keep working via a coalesce fallback in queryAttribution.

import type { MicroUSD } from "./types";

export interface AttributionFilters {
  tag: string | null;
  provider: "openai" | "anthropic" | null;
  outcome: "conversion" | "positive_feedback" | "session_engaged" | null;
}

// -------------------------------------------------------------------------------------------
// What the breakdown dimension actually is (#320, item 3).
//
// The rows are `gen_ai.system` values, and on a vector span that attribute is the vector vendor:
// pinecone, weaviate, qdrant. The SDK is right to emit it that way (it is what the OTel semantic
// convention says), and the page was wrong to call the resulting column "Provider", which reads as
// "LLM provider" and made pinecone look like one.
//
// We relabel rather than filter, and the reason is the money. This is a cost-attribution view: the
// cost column is real spend, and the totals and per-conversion ratios are aggregated upstream in
// queryAttribution over the same span set. Dropping the vector rows in the presentation layer would
// hide real money from a page whose whole job is to account for it, and would leave the visible
// rows summing to less than the total beside them. Naming the dimension honestly costs nothing and
// hides nothing. Scoping the QUERY to LLM systems is the real fix if the view should only ever be
// about LLM spend, and that belongs in the ClickHouse layer, not here.
// -------------------------------------------------------------------------------------------

/**
 * `gen_ai.system` values that name a vector store rather than an LLM provider. Used only to tag a
 * row in the UI, never to drop one: an unrecognised system is left untagged rather than guessed at.
 */
export const VECTOR_SYSTEMS: readonly string[] = [
  "pinecone",
  "weaviate",
  "qdrant",
  "chroma",
  "milvus",
  "pgvector",
  "opensearch",
  "elasticsearch",
];

/** "vector" for a known vector store, "llm" otherwise. Presentation only (#320). */
export function systemKind(system: string): "llm" | "vector" {
  return VECTOR_SYSTEMS.includes(system.trim().toLowerCase()) ? "vector" : "llm";
}

export interface ProviderAttribution {
  /**
   * The span's `gen_ai.system`: an LLM provider on an LLM span, the vector vendor on a vector span.
   * The field name is the wire shape and stays; the UI label is "System", not "Provider" (#320).
   */
  provider: string; // "openai" | "anthropic" | "pinecone" | "unknown" | ...
  sessions: number;
  conversions: number;
  costMicroUsd: MicroUSD;
  // Headline: $/conversion. NaN when conversions === 0 (the page renders "—").
  costPerConversionMicroUsd: MicroUSD | null;
  // 95% Wilson interval on conversion rate. lo/hi are absolute rates (0..1).
  conversionRate: number;
  conversionRateLo: number;
  conversionRateHi: number;
  // Revenue per distinct user, from the money-typed business_events of the tenant's configured
  // revenue sources (CTO-110, CTO-194 — no longer Stripe-only). Null when no revenue events exist
  // yet for the tenant — we surface "—" rather than fabricate.
  valuePerUserMicroUsd: MicroUSD | null;
  // Margin per distinct user = value/user − cost/user. Null when value/user is null.
  marginPerUserMicroUsd: MicroUSD | null;
  // Margin as a fraction of value: (value − cost) / value. Null when value/user is null or 0.
  marginPct: number | null;
}

/** One calendar day of LLM cost split by provider, for the provider-breakdown chart (CTO-223). */
export interface ProviderCostDay {
  /** ISO yyyy-mm-dd, from ClickHouse's window bounds (never the Node clock, per CTO-203). */
  date: string;
  /** Micro-USD per provider for this day. A missing provider is a real zero. */
  byProvider: Record<string, MicroUSD>;
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
  /**
   * Daily LLM cost per provider across the window, oldest to newest, for the stacked provider chart.
   * Optional so the mock/empty reports and any older consumer stay valid; the page renders no chart
   * when it is absent rather than fabricating a series.
   */
  dailyByProvider?: ProviderCostDay[];
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
  // Optional revenue side — provider rows are unchanged when no Stripe data exists.
  revenue?: { revenueMicroUsd: MicroUSD; distinctUsers: number } | null,
  /**
   * Spans for this provider that could not be priced (CTO-244). Non-zero means `costMicroUsd` is a
   * lower bound, so every per-unit figure derived from it is understated by an unknowable amount:
   * the cost numerator skips those spans while the conversion and user denominators still count
   * them. Those ratios are nulled rather than published; the sum itself is kept because it is real
   * money already spent, and the caller reports the coverage alongside it.
   */
  unpricedSpans = 0,
): ProviderAttribution {
  const { p, lo, hi } = wilsonInterval(conversions, sessions);
  const costKnown = unpricedSpans === 0;
  const costPerConversion =
    costKnown && conversions > 0 ? Math.round(costMicroUsd / conversions) : null;
  // Revenue lights up only when Stripe events exist for *this* provider. Without users we have
  // no denominator, so the row stays honest with nulls.
  let valuePerUser: MicroUSD | null = null;
  let marginPerUser: MicroUSD | null = null;
  let marginPct: number | null = null;
  if (costKnown && revenue && revenue.distinctUsers > 0 && revenue.revenueMicroUsd !== 0) {
    valuePerUser = Math.round(revenue.revenueMicroUsd / revenue.distinctUsers);
    const costPerUser = Math.round(costMicroUsd / revenue.distinctUsers);
    marginPerUser = valuePerUser - costPerUser;
    marginPct = valuePerUser > 0 ? (valuePerUser - costPerUser) / valuePerUser : null;
  }
  return {
    provider,
    sessions,
    conversions,
    costMicroUsd,
    costPerConversionMicroUsd: costPerConversion,
    conversionRate: p,
    conversionRateLo: lo,
    conversionRateHi: hi,
    valuePerUserMicroUsd: valuePerUser,
    marginPerUserMicroUsd: marginPerUser,
    marginPct,
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
  // Demo seed numbers: a YC-stage SaaS with two providers in production, Stripe wired.
  // openai: 3200 sessions, 412 conversions, $24K LLM cost, 240 paying users → $200 ARPA, 50% margin
  // anthropic: 2100 sessions, 318 conversions, $14.2K LLM cost, 195 paying users → $267 ARPA, 73% margin
  // Anthropic is cheaper per conversion AND has higher value/user: the kind of finding /attribution exists to surface.
  const perProvider = [
    buildProviderRow("openai", 3200, 412, 24_000_000_000, {
      revenueMicroUsd: 48_000_000_000,
      distinctUsers: 240,
    }),
    buildProviderRow("anthropic", 2100, 318, 14_200_000_000, {
      revenueMicroUsd: 52_000_000_000,
      distinctUsers: 195,
    }),
  ];
  const sessions = perProvider.reduce((s, p) => s + p.sessions, 0);
  const conversions = perProvider.reduce((s, p) => s + p.conversions, 0);
  const costMicroUsd = perProvider.reduce((s, p) => s + p.costMicroUsd, 0);
  const totals: AttributionReport["totals"] = {
    sessions,
    conversions,
    costMicroUsd,
    costPerConversionMicroUsd:
      conversions > 0 ? Math.round(costMicroUsd / conversions) : null,
  };
  // A 30-day synthetic per-provider series so the preview chart renders something shaped like real
  // traffic. Only ever shown behind the SAMPLE DATA banner (isMock), and a mild deterministic wave
  // per provider keeps the bars from looking like a flat fabrication.
  const days = 30;
  const dailyByProvider: ProviderCostDay[] = [];
  const anchor = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(anchor);
    d.setUTCDate(anchor.getUTCDate() - i);
    const date = d.toISOString().slice(0, 10);
    const byProvider: Record<string, MicroUSD> = {};
    for (const p of perProvider) {
      const base = p.costMicroUsd / days;
      const wave = 1 + 0.25 * Math.sin((i / days) * Math.PI * 2);
      byProvider[p.provider] = Math.round(base * wave);
    }
    dailyByProvider.push({ date, byProvider });
  }
  return { filters, perProvider, totals, dailyByProvider, isMock: true };
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
