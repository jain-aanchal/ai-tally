// SPDX-License-Identifier: Apache-2.0
// CAC period type + gateway-backed fetch (CTO-111).
//
// Shapes match the gateway's /v1/tenant/cac response. Money is micro-USD on the wire (matching
// business_events). The TS types use ``MicroUsd = number`` for clarity at call sites.

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";
const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";

export interface CacPeriod {
  /** ISO date — first day of the month, e.g. "2026-01-01". */
  periodStart: string;
  periodEnd: string;
  currency: string;
  paidSpendMicroUsd: number;
  salesSpendMicroUsd: number;
  contentSpendMicroUsd: number;
  overheadMicroUsd: number;
  newCustomersPaid: number;
  newCustomersTotal: number;
  notes: string | null;
  closedAt: string | null;
  locked: boolean;
}

interface CacApiPeriod {
  period_start: string;
  period_end: string;
  currency: string;
  paid_spend_micro_usd: number;
  sales_spend_micro_usd: number;
  content_spend_micro_usd: number;
  overhead_micro_usd: number;
  new_customers_paid: number;
  new_customers_total: number;
  notes: string | null;
  closed_at: string | null;
  locked: boolean;
}

function fromApi(p: CacApiPeriod): CacPeriod {
  return {
    periodStart: p.period_start,
    periodEnd: p.period_end,
    currency: p.currency,
    paidSpendMicroUsd: p.paid_spend_micro_usd,
    salesSpendMicroUsd: p.sales_spend_micro_usd,
    contentSpendMicroUsd: p.content_spend_micro_usd,
    overheadMicroUsd: p.overhead_micro_usd,
    newCustomersPaid: p.new_customers_paid,
    newCustomersTotal: p.new_customers_total,
    notes: p.notes,
    closedAt: p.closed_at,
    locked: p.locked,
  };
}

/**
 * Fetch all CAC periods for the tenant, newest first. Returns ``[]`` when the gateway is
 * unreachable or has no data — the page falls back to its "Need 3+ months" empty state, which is
 * the right behavior for CI / fresh clones.
 */
export async function queryCacPeriods(): Promise<CacPeriod[]> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/cac`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return [];
    const body = (await res.json()) as { periods: CacApiPeriod[] };
    return (body.periods ?? []).map(fromApi);
  } catch {
    return [];
  }
}

/**
 * Page-level economic assumptions that do NOT come from the gateway's cac_periods wire shape.
 *
 * CAC periods carry spend + customer counts only. Computing payback and LTV additionally needs the
 * revenue side — ARPA (average revenue per account / month) and gross margin — which the CAC
 * backend does not store. Until a revenue source is wired (the Stripe-backed business_events table,
 * see unitEconomics.ts `valuePerUser`), the page reads these from a per-period companion record.
 *
 * Kept deliberately separate from `CacPeriod` so we never imply the gateway returns these: a period
 * with no economics record renders payback/LTV as "—" (honest-null), never a fabricated number.
 */
export interface PeriodEconomics {
  /** Average revenue per account, per month, in micro-USD. */
  arpaMicroUsd: number;
  /** Gross margin as a fraction in [0,1], e.g. 0.78 for 78%. */
  grossMarginPct: number;
  /** Expected retention in months — drives LTV. */
  retentionMonths: number;
}

/**
 * Mock CAC periods for local dev / CI / fresh clones, used when the gateway is unreachable.
 * CLEARLY LABELLED MOCK — not real telemetry. Newest-first, mirroring the gateway's
 * `queryCacPeriods` ordering. The latest two months are unlocked (still editable); older months are
 * `locked` (prior-month-closed). One month deliberately omits its economics record so the page's
 * honest-null path (payback/LTV → "—") is exercised by the demo, and one has zero paid customers so
 * the marketing-CAC divide-by-zero guard ("—") is exercised too.
 */
export const MOCK_CAC_PERIODS: CacPeriod[] = [
  {
    periodStart: "2026-05-01",
    periodEnd: "2026-05-31",
    currency: "USD",
    paidSpendMicroUsd: 42_000_000_000,
    salesSpendMicroUsd: 28_000_000_000,
    contentSpendMicroUsd: 9_000_000_000,
    overheadMicroUsd: 31_000_000_000,
    newCustomersPaid: 38,
    newCustomersTotal: 61,
    notes: "Spring campaign tail-off",
    closedAt: null,
    locked: false,
  },
  {
    periodStart: "2026-04-01",
    periodEnd: "2026-04-30",
    currency: "USD",
    paidSpendMicroUsd: 39_500_000_000,
    salesSpendMicroUsd: 26_000_000_000,
    contentSpendMicroUsd: 8_500_000_000,
    overheadMicroUsd: 30_000_000_000,
    newCustomersPaid: 41,
    newCustomersTotal: 66,
    notes: null,
    closedAt: null,
    locked: false,
  },
  {
    periodStart: "2026-03-01",
    periodEnd: "2026-03-31",
    currency: "USD",
    paidSpendMicroUsd: 44_000_000_000,
    salesSpendMicroUsd: 24_000_000_000,
    contentSpendMicroUsd: 8_000_000_000,
    overheadMicroUsd: 29_000_000_000,
    newCustomersPaid: 33,
    newCustomersTotal: 52,
    // No economics record below for this month → payback/LTV render "—" (honest-null demo).
    notes: "ARPA reconciliation pending",
    closedAt: "2026-04-03T00:00:00Z",
    locked: true,
  },
  {
    periodStart: "2026-02-01",
    periodEnd: "2026-02-28",
    currency: "USD",
    paidSpendMicroUsd: 36_000_000_000,
    salesSpendMicroUsd: 22_000_000_000,
    contentSpendMicroUsd: 7_000_000_000,
    overheadMicroUsd: 28_000_000_000,
    newCustomersPaid: 0,
    newCustomersTotal: 44,
    // Zero paid customers → marketing CAC renders "—" (divide-by-zero guard in the lib).
    notes: "Paid channels paused mid-month",
    closedAt: "2026-03-04T00:00:00Z",
    locked: true,
  },
];

/** Per-period economics keyed by `periodStart`. March is intentionally absent (honest-null demo). */
export const MOCK_PERIOD_ECONOMICS: Record<string, PeriodEconomics> = {
  "2026-05-01": { arpaMicroUsd: 220_000_000, grossMarginPct: 0.78, retentionMonths: 24 },
  "2026-04-01": { arpaMicroUsd: 215_000_000, grossMarginPct: 0.77, retentionMonths: 24 },
  "2026-02-01": { arpaMicroUsd: 205_000_000, grossMarginPct: 0.75, retentionMonths: 22 },
};

/**
 * Default first month finance should fill — the *next* un-entered month.
 *
 * Finance fills serially, in chronological order; the form should not default to the current
 * month (gives the impression the previous month is already done) and definitely not default to
 * an arbitrary past month. We compute one-after-the-latest-entered, or "this month" when the
 * tenant has no rows yet.
 */
export function nextUnenteredPeriodStart(
  existing: CacPeriod[],
  today: Date = new Date(),
): string {
  if (existing.length === 0) {
    const d = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1));
    return d.toISOString().slice(0, 10);
  }
  // existing is sorted desc, so [0] is the latest.
  const latest = existing[0].periodStart;
  const [y, m] = latest.split("-").map((s) => parseInt(s, 10));
  const next = new Date(Date.UTC(y, m, 1)); // m is 1-indexed; this is the next month
  return next.toISOString().slice(0, 10);
}
