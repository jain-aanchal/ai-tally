// SPDX-License-Identifier: Apache-2.0
// Third-party integration display catalog + state derivation (CTO-117).
//
// The /connectors page shows a card per supported third-party integration (Stripe, Segment,
// HubSpot, Pendo, …) with three honest states:
//
//   - "healthy" — last run succeeded, real event count and relative age shown
//   - "failing" — last run failed (or partial), shows last successful sync + a truncated error
//   - "not-connected" — no rows yet for this tenant, gray CTA, no fabricated numbers
//
// The catalog lives here (rather than on web/lib/connectors.ts) so the cost-layer connector list
// stays narrowly about CTO-63/68 and this stays narrowly about third-party webhooks/pollers.

import type { IntegrationStatusRow } from "./clickhouse";

export type IntegrationState = "healthy" | "failing" | "not-connected";

export interface IntegrationDef {
  /** Stable id — matches the gateway's connector_id and the backend worker's name. */
  id: string;
  name: string;
  blurb: string;
  /** Where to send a tenant who clicks "Connect" when there's no row yet. */
  setupHref: string;
}

/**
 * The third-party integrations we surface cards for. Stripe is wired today (CTO-110 webhook ⇒
 * record_run); Segment, HubSpot, and Pendo are placeholders that will light up as their workers
 * land and call record_run. Order is deliberate — Stripe first because it's the only one with
 * real data flowing on day one.
 */
export const INTEGRATIONS: IntegrationDef[] = [
  {
    id: "stripe",
    name: "Stripe",
    blurb: "Payments, subscriptions, refunds via webhook. Lights up automatically once you connect.",
    // The Stripe tile owns its own connect flow (paste-the-signing-secret). When the card shows
    // "Not connected" it should anchor the tenant to that tile, which lives further down the page.
    setupHref: "#stripe-tile",
  },
  {
    id: "segment",
    name: "Segment",
    blurb: "CDP track events. Worker pulls from your write key on a fixed schedule.",
    setupHref: "https://docs.example.com/integrations/segment",
  },
  {
    id: "hubspot",
    name: "HubSpot",
    blurb: "Deal-stage / lifecycle changes for revenue attribution. OAuth-based poller.",
    setupHref: "https://docs.example.com/integrations/hubspot",
  },
  {
    id: "pendo",
    name: "Pendo",
    blurb: "Product-analytics signals for activation cohorts. Polled hourly.",
    setupHref: "https://docs.example.com/integrations/pendo",
  },
];

/** Truncate an error message for the card preview. Full message reveals on the details click. */
export const ERROR_PREVIEW_MAX = 80;

export function truncateError(msg: string | null | undefined, max = ERROR_PREVIEW_MAX): string {
  if (!msg) return "";
  return msg.length > max ? msg.slice(0, max - 1).trimEnd() + "…" : msg;
}

export interface IntegrationCardView {
  def: IntegrationDef;
  state: IntegrationState;
  /** Populated only when state is "healthy" or "failing". */
  row: IntegrationStatusRow | null;
}

/**
 * Merge the static catalog with the per-tenant rows returned by the gateway. Pure / deterministic
 * so the page can be unit-tested without a fetch.
 *
 * Tenants with no row for a given integration get ``state: "not-connected"`` — the honest default.
 * A row with ``last_run_status === "success"`` is "healthy"; ``"failed"`` or ``"partial"`` is
 * "failing".
 */
export function applyIntegrationStatus(
  catalog: IntegrationDef[],
  rows: IntegrationStatusRow[],
): IntegrationCardView[] {
  const byId = new Map<string, IntegrationStatusRow>();
  for (const r of rows) byId.set(r.connector_id, r);
  return catalog.map((def) => {
    const row = byId.get(def.id) ?? null;
    if (row === null) return { def, state: "not-connected", row: null };
    const state: IntegrationState = row.last_run_status === "success" ? "healthy" : "failing";
    return { def, state, row };
  });
}
