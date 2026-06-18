// SPDX-License-Identifier: Apache-2.0
// Unit-economics formulas (CTO-111). Pure functions, no I/O.
//
// CAC has three "flavors" people argue about endlessly:
//   * Marketing CAC — paid spend per paid customer. The narrowest cut; ignores the org cost of
//     converting free trials, salaried AEs, and the content team.
//   * Blended CAC   — paid + sales + content per *total* customer (paid + free). Closer to what
//     the business actually spent to land a logo.
//   * Fully-loaded  — adds overhead (the team's salaries, tools, allocated rent). What the CFO
//     defends to the board.
// We surface all three with explicit labels so nobody has to guess which one the dashboard means.
//
// Payback is honest: when margin per user is <=0 the business is losing money per customer, and
// "payback" is undefined. Returning null forces the UI to render "—" rather than 0/Infinity/NaN.

import type { CacPeriod } from "./cac";

/** Marketing CAC: paid spend / new *paid* customers. Null when denominator is 0. */
export function marketingCac(p: CacPeriod): number | null {
  if (p.newCustomersPaid <= 0) return null;
  return p.paidSpendMicroUsd / p.newCustomersPaid;
}

/** Blended CAC: (paid + sales + content) / new *total* customers. Null when denominator is 0. */
export function blendedCac(p: CacPeriod): number | null {
  if (p.newCustomersTotal <= 0) return null;
  return (
    (p.paidSpendMicroUsd + p.salesSpendMicroUsd + p.contentSpendMicroUsd) /
    p.newCustomersTotal
  );
}

/** Fully-loaded CAC: blended + overhead, divided by total customers. */
export function fullyLoadedCac(p: CacPeriod): number | null {
  if (p.newCustomersTotal <= 0) return null;
  return (
    (p.paidSpendMicroUsd +
      p.salesSpendMicroUsd +
      p.contentSpendMicroUsd +
      p.overheadMicroUsd) /
    p.newCustomersTotal
  );
}

/**
 * Cost per user for the period. ``totalCostFromOtelSpans`` is the dashboard's existing
 * cost-of-serve number from ClickHouse (LLM + tools + … per the cost page) for the same window;
 * we divide by total customers to get unit cost-of-serve.
 */
export function costPerUser(p: CacPeriod, totalCostFromOtelSpans: number): number | null {
  if (p.newCustomersTotal <= 0) return null;
  return totalCostFromOtelSpans / p.newCustomersTotal;
}

/**
 * Value per user for the period. ``totalRevenueFromBusinessEvents`` comes from the Stripe-backed
 * business_events table (CTO-110) summed across the period.
 */
export function valuePerUser(p: CacPeriod, totalRevenueFromBusinessEvents: number): number | null {
  if (p.newCustomersTotal <= 0) return null;
  return totalRevenueFromBusinessEvents / p.newCustomersTotal;
}

/**
 * Margin per user. CAN BE NEGATIVE — we deliberately don't clamp. A business that pays more to
 * serve each customer than it earns is the headline the dashboard exists to show.
 */
export function marginPerUser(value: number | null, cost: number | null): number | null {
  if (value === null || cost === null) return null;
  return value - cost;
}

/** Margin as a fraction of value. Null when value is 0 (no denominator to divide by). */
export function marginPct(value: number | null, cost: number | null): number | null {
  if (value === null || cost === null) return null;
  if (value === 0) return null;
  return (value - cost) / value;
}

/**
 * Payback months: how many months of contribution margin recoup the CAC. Null when margin <=0 —
 * the business is currently losing money per user, so the answer isn't "Infinity months", it's
 * "this formula doesn't apply". The UI renders "—" in that case.
 */
export function paybackMonths(cac: number | null, margin: number | null): number | null {
  if (cac === null || margin === null) return null;
  if (margin <= 0) return null;
  return cac / margin;
}

/** LTV: contribution margin × expected retention months. Negative margin → negative LTV (honest). */
export function ltv(margin: number | null, retentionMonths: number): number | null {
  if (margin === null) return null;
  return margin * retentionMonths;
}

/** LTV/CAC ratio. Null when CAC is 0 or null. Can be negative (LTV is negative). */
export function ltvOverCac(ltvValue: number | null, cac: number | null): number | null {
  if (ltvValue === null || cac === null || cac === 0) return null;
  return ltvValue / cac;
}

/** Color band for LTV/CAC. B2B SaaS defaults — tenant-configurable in v2. */
export function ltvCacBand(ratio: number | null): "green" | "yellow" | "red" | "unknown" {
  if (ratio === null) return "unknown";
  if (ratio > 3.0) return "green";
  if (ratio >= 1.0) return "yellow";
  return "red";
}
