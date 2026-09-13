// SPDX-License-Identifier: Apache-2.0
// The per-organization switch for the hosted edge proxy (zero-code connect). Server-only.
//
// Off by default: routing production LLM calls through ai-tally's server is a trust decision an org
// admin makes explicitly. The gateway owns the setting (tenant_proxy_config, 0033) and carries it to
// running proxies through the edge-key feed, so the dashboard never talks to a proxy directly.
import { controlPlaneHeaders } from "./getTenant";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

interface ProxyConfigResponse {
  tenant_id: string;
  config: { enabled: boolean; updated_at: string | null };
}

/**
 * Whether the hosted proxy is on for this tenant, or null when the gateway could not say.
 *
 * Null is not false. Rendering an unreadable setting as "Off" would tell an admin their proxy is
 * disabled when it may be serving traffic, which is the one wrong answer this switch must never give.
 */
export async function queryProxyEnabled(tenantId: string): Promise<boolean | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/proxy/config`, {
      headers: controlPlaneHeaders(tenantId),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as ProxyConfigResponse;
    return typeof body.config?.enabled === "boolean" ? body.config.enabled : null;
  } catch {
    return null;
  }
}
