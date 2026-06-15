// SPDX-License-Identifier: Apache-2.0
// Per-tenant cost-layer connector declarations (CTO-107).
//
// The "Partial data" banner used to fire whenever any cost layer reported zero, which made it
// permanent on every demo (only the LLM connector is wired today) and trained users to ignore it.
// We fix that by asking the gateway which connectors this tenant has *declared* enabled, and only
// counting those layers when computing partiality.
//
// Server-only: imported by Route Handlers. The gateway is the source of truth for the per-tenant
// config — we never read Postgres directly from the dashboard. Failures fall back to ["llm"] so
// demos and CI (where the gateway may be unreachable) keep working and the banner stays quiet.
//
// Tenant scoping mirrors web/lib/clickhouse.ts: TALLY_TENANT_ID || "local-dev".
import { LAYERS, type Layer } from "./cost";

const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";
const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

interface TenantConnectorsResponse {
  tenant_id: string;
  connectors: Array<{
    layer: string;
    enabled: boolean;
    enabled_at: string;
    disabled_at: string | null;
    notes: string | null;
  }>;
  enabled_layers: string[];
}

/** Fallback used when the gateway is unreachable — matches the only connector wired today. */
export const DEFAULT_ENABLED_LAYERS: Layer[] = ["llm"];

function asLayer(s: string): Layer | null {
  return (LAYERS as readonly string[]).includes(s) ? (s as Layer) : null;
}

/**
 * Ask the gateway which cost-layer connectors this tenant has declared enabled.
 *
 * The dashboard uses this list — and only this list — to decide whether the "Partial data" banner
 * should fire. A layer that was never declared isn't a gap; it's by design.
 *
 * When the gateway is unreachable (CI, fresh clone with nothing running, transient outage) we
 * return ``["llm"]`` so a demo doesn't suddenly start screaming "partial data" — which would
 * defeat the purpose of this whole ticket.
 */
export async function queryEnabledConnectors(): Promise<Layer[]> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/connectors`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      // Short timeout: a slow gateway shouldn't block every page render.
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      console.warn(`[tenant] /v1/tenant/connectors HTTP ${res.status}; falling back`);
      return [...DEFAULT_ENABLED_LAYERS];
    }
    const body = (await res.json()) as TenantConnectorsResponse;
    const layers: Layer[] = [];
    for (const s of body.enabled_layers ?? []) {
      const l = asLayer(s);
      if (l) layers.push(l);
    }
    // A tenant that exists but has declared nothing is treated like the fallback — otherwise the
    // banner would fire for *every* layer the moment any data lands. The intent is "no declared
    // connectors = no expectations", and the LLM connector is the implicit baseline.
    if (layers.length === 0) return [...DEFAULT_ENABLED_LAYERS];
    return layers;
  } catch (err) {
    console.warn(
      "[tenant] /v1/tenant/connectors unreachable, falling back:",
      (err as Error).message,
    );
    return [...DEFAULT_ENABLED_LAYERS];
  }
}

/**
 * Enable or disable a single cost-layer connector for the current tenant.
 *
 * POSTs to the gateway's /v1/tenant/connectors. Returns ``{ ok: true }`` on success and
 * ``{ ok: false, error: "..." }`` when the gateway is unreachable or rejects the layer — callers
 * use this to render an inline confirmation/error without taking down the page.
 */
export async function setConnectorEnabled(
  layer: Layer,
  enabled: boolean,
): Promise<{ ok: true } | { ok: false; error: string }> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/connectors`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-tenant-id": TENANT,
      },
      body: JSON.stringify({ layer, enabled }),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      return { ok: false, error: `gateway HTTP ${res.status}${text ? `: ${text}` : ""}` };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}
