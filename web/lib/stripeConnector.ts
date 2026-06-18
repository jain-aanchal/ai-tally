// SPDX-License-Identifier: Apache-2.0
// Per-tenant Stripe connector config (CTO-110).
//
// Mirrors lib/tenant.ts: the dashboard never talks to Postgres directly — it goes through the
// gateway's /v1/tenant/stripe + /v1/tenant/stripe/connect endpoints. Failure modes fall back
// gracefully so a /connectors page render never breaks because the gateway is briefly down.

const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";
const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

export interface StripeConfigView {
  // The dashboard sees a fingerprint only — never the raw secret. The gateway derives this from
  // the persisted secret on every read.
  secretFingerprint: string | null;
  stripeAccountId: string | null;
  connectedAt: string | null;
  disconnectedAt: string | null;
  isActive: boolean;
}

interface StripeGetResponse {
  tenant_id: string;
  stripe: {
    tenant_id: string;
    stripe_account_id: string | null;
    secret_fingerprint: string | null;
    connected_at: string;
    disconnected_at: string | null;
    is_active: boolean;
  } | null;
}

export async function queryStripeConfig(): Promise<StripeConfigView | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/stripe`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as StripeGetResponse;
    if (!body.stripe) return null;
    return {
      secretFingerprint: body.stripe.secret_fingerprint,
      stripeAccountId: body.stripe.stripe_account_id,
      connectedAt: body.stripe.connected_at || null,
      disconnectedAt: body.stripe.disconnected_at,
      isActive: body.stripe.is_active,
    };
  } catch {
    return null;
  }
}

export async function connectStripe(
  webhookSecret: string,
  stripeAccountId: string | null,
): Promise<{ ok: true; fingerprint: string | null } | { ok: false; error: string }> {
  if (!webhookSecret.startsWith("whsec_")) {
    return { ok: false, error: "Signing secret must start with 'whsec_'" };
  }
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/stripe/connect`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-tenant-id": TENANT,
      },
      body: JSON.stringify({
        webhook_secret: webhookSecret,
        stripe_account_id: stripeAccountId || undefined,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      return { ok: false, error: `gateway HTTP ${res.status}${text ? `: ${text}` : ""}` };
    }
    const body = (await res.json()) as {
      stripe: { secret_fingerprint: string | null };
    };
    return { ok: true, fingerprint: body.stripe?.secret_fingerprint ?? null };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

/** Public URL the tenant configures in their Stripe dashboard. */
export function webhookUrl(): string {
  const tenant = TENANT;
  // The gateway is reachable at the same URL we use server-side; the tenant runs the curl
  // documented in RUNNING.md → `stripe listen --forward-to <this URL>` for local testing.
  return `${GATEWAY_URL}/v1/stripe/webhook?tenant=${encodeURIComponent(tenant)}`;
}
