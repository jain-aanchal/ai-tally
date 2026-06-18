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
