// SPDX-License-Identifier: Apache-2.0
"use server";

// Server action for the Stripe tile (CTO-110). Persists the per-tenant webhook signing secret
// via the gateway's /v1/tenant/stripe/connect endpoint. The raw secret never round-trips back
// to the client — the gateway returns a fingerprint, which is what the UI shows from then on.
import { revalidatePath } from "next/cache";

import { connectStripe } from "@/lib/stripeConnector";

export interface ConnectStripeResult {
  ok: boolean;
  error?: string;
  fingerprint?: string | null;
}

export async function connectStripeAction(
  webhookSecret: string,
  stripeAccountId: string | null,
): Promise<ConnectStripeResult> {
  const result = await connectStripe(webhookSecret, stripeAccountId);
  if (!result.ok) return { ok: false, error: result.error };
  // The connectors page is what shows the connected state — revalidate so a hard refresh
  // isn't needed.
  revalidatePath("/connectors");
  return { ok: true, fingerprint: result.fingerprint };
}
