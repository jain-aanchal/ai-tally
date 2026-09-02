// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "./getTenant";
// Cloud cost-connector configuration (CTO-176).
//
// Mirrors lib/stripeConnector.ts: the dashboard never talks to Postgres directly, it goes through
// the gateway's /v1/tenant/cost-connectors endpoints. Every credential field here is a secret
// manager REFERENCE, never a raw key, which is all the underlying columns are allowed to hold.

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

// Client-safe constants, types and pure helpers moved to costConnectorsShared.ts (CTO-259) so the
// client table can import them without reaching this server-only module. Re-exported so existing
// server call sites keep importing from "@/lib/costConnectors".
export {
  CONFIGURABLE,
  type ConfigurableConnector,
  isConfigurable,
  type CostConnectorConfig,
} from "./costConnectorsShared";
import type { ConfigurableConnector, CostConnectorConfig } from "./costConnectorsShared";

interface ConfigWire {
  connector: string;
  configured: boolean;
  credentials_ref: string | null;
  is_reference: boolean;
  details: Record<string, unknown>;
  last_run_at: string | null;
  last_status: string | null;
  connected_at: string | null;
}

/** Configured connectors for the tenant. `null` means the gateway is unreachable. */
export async function queryCostConnectorConfigs(): Promise<CostConnectorConfig[] | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/cost-connectors`, {
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { configs: ConfigWire[] };
    return (body.configs ?? []).map((c) => ({
      connector: c.connector,
      configured: c.configured,
      credentialsRef: c.credentials_ref,
      isReference: c.is_reference,
      details: c.details ?? {},
      lastRunAt: c.last_run_at,
      lastStatus: c.last_status,
      connectedAt: c.connected_at,
    }));
  } catch {
    return null;
  }
}

export type ConnectResult =
  | { ok: true; note: string | null }
  | { ok: false; error: string };

/** Create or replace one connector's config. Field validation lives in the gateway. */
export async function connectCostConnector(
  connector: ConfigurableConnector,
  fields: Record<string, unknown>,
): Promise<ConnectResult> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/cost-connectors`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify({ connector, ...fields }),
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });
    if (!res.ok) {
      // The gateway returns operator-facing validation text in `detail`; surface it verbatim so a
      // missing usd_per_gb reads as the real reason instead of a status code.
      const detail = await res
        .json()
        .then((b: { detail?: string }) => b?.detail)
        .catch(() => undefined);
      return { ok: false, error: detail || `gateway HTTP ${res.status}` };
    }
    const body = (await res.json()) as { note?: string | null };
    return { ok: true, note: body.note ?? null };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

/** Remove a connector's config. Idempotent. */
export async function disconnectCostConnector(
  connector: ConfigurableConnector,
): Promise<ConnectResult> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/cost-connectors/${connector}`, {
      method: "DELETE",
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });
    if (!res.ok) return { ok: false, error: `gateway HTTP ${res.status}` };
    return { ok: true, note: null };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}
