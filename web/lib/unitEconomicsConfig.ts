// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "./getTenant";
// Per-tenant LTV/CAC band threshold overrides, read via the gateway (CTO-126).
//
// The band cutoffs used to be hardcoded B2B-SaaS defaults inline in unitEconomics.ts. They are now
// tenant-configurable: the gateway persists a per-tenant row (GET/POST /v1/tenant/unit-economics/
// config). This module is the server-only reader — the web app never touches Postgres directly, same
// rule as web/lib/cac.ts and web/lib/tenant.ts. A tenant with no row (or an unreachable gateway on
// CI / fresh clones) yields `null`, which the classify helpers treat as "use the defaults".

import type { UnitEconomicsThresholdOverrides } from "./unitEconomics";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** Wire shape of the gateway's config object (snake_case, matching the Postgres columns). */
export interface UnitEconomicsConfigApi {
  ltv_cac_green_threshold: number;
  ltv_cac_yellow_threshold: number;
  payback_months_green: number;
  payback_months_yellow: number;
  created_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

/** Map the gateway's snake_case config to the lib's camelCase override shape. */
export function overridesFromApi(
  cfg: UnitEconomicsConfigApi | null,
): UnitEconomicsThresholdOverrides | null {
  if (!cfg) return null;
  return {
    ltvCacGreen: cfg.ltv_cac_green_threshold,
    ltvCacYellow: cfg.ltv_cac_yellow_threshold,
    paybackGreen: cfg.payback_months_green,
    paybackYellow: cfg.payback_months_yellow,
  };
}

/**
 * Fetch the tenant's threshold overrides. Returns `null` when the tenant has no row OR the gateway
 * is unreachable — callers pass that straight to `resolveThresholds`, which falls back to defaults.
 */
export async function queryUnitEconomicsConfig(): Promise<UnitEconomicsThresholdOverrides | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/unit-economics/config`, {
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { config: UnitEconomicsConfigApi | null };
    return overridesFromApi(body.config ?? null);
  } catch {
    return null;
  }
}
