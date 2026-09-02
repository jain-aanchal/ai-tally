// SPDX-License-Identifier: Apache-2.0
import { resolveTenantId } from "./getTenant";
// Dashboard client for the account control plane (CTO-188, plan D2).
//
// Two gateway endpoints, one module, because they answer the two halves of the same question "which
// customer is this row?":
//
//   * B7 `/v1/tenant/account-labels` — the optional human-readable name for a hash. Labels live in
//     Postgres and are joined at render time, so ClickHouse never holds a customer name.
//   * B6 `/v1/tenant/account-lookup` — plaintext account id to hash, the forward direction of a
//     one-way function. Without it an UNLABELLED account cannot be found at all and the tab is a
//     list of opaque hex.
//
// Server-only, like lib/tenant.ts: the gateway URL and tenant scoping are server config, and the
// lookup body carries a customer identifier that has no business travelling from a browser.
//
// The plaintext account id is handled the way the gateway handles it: used for one call and then
// dropped. It is never cached, never returned in an error message, and never logged. Every warning
// below reports the transport failure only.

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** A slow gateway must not hold a page render open. Same budget lib/tenant.ts uses. */
const TIMEOUT_MS = 2000;

/** One `(account hash) -> label` mapping, as the tab consumes it. */
export interface AccountLabel {
  accountIdHash: string;
  label: string;
  updatedAt: string;
}

interface AccountLabelsResponse {
  tenant_id: string;
  labels: Array<{ account_id_hash: string; label: string; updated_at: string }>;
}

interface AccountLookupResponse {
  tenant_id: string;
  account_id_hash: string;
  key_version: string;
  hashes: Array<{ account_id_hash: string; key_version: string; tenant_key_form: string }>;
}

/**
 * Every label this tenant has set, keyed by account hash.
 *
 * Returns `null` when the gateway is unreachable or refuses, which the page renders as "labels
 * unavailable, showing hashes" rather than as "this tenant has no labels". The two look identical
 * in a table of hex and only one of them is the tenant's own choice.
 */
export async function queryAccountLabels(): Promise<Map<string, string> | null> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/account-labels`, {
      headers: { "x-tenant-id": await resolveTenantId() },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) {
      console.warn(`[accounts] /v1/tenant/account-labels HTTP ${res.status}; rendering hashes`);
      return null;
    }
    const body = (await res.json()) as AccountLabelsResponse;
    const out = new Map<string, string>();
    for (const row of body.labels ?? []) {
      if (row?.account_id_hash && row?.label) out.set(row.account_id_hash, row.label);
    }
    return out;
  } catch (err) {
    console.warn("[accounts] /v1/tenant/account-labels unreachable:", (err as Error).message);
    return null;
  }
}

export type AccountLookup =
  | {
      ok: true;
      /**
       * EVERY hash the account could have been emitted under, not one.
       *
       * The tenant identifier is HMAC key material, so `local-dev` and its `tenants.id` UUID derive
       * different key spaces and the endpoint deliberately refuses to pick a winner. Spans ingested
       * through one door carry one digest and spans through the other carry another, for the same
       * customer. Matching on `hashes[0]` alone would find the account only half the time, and the
       * half it missed would look like a customer with no spend.
       */
      hashes: string[];
    }
  | { ok: false; error: string };

/**
 * Plaintext account id to candidate hashes, via the gateway's own HMAC keys.
 *
 * An id nobody has ever emitted is not an error here, exactly as it is not one at the endpoint: it
 * comes back `ok` with well-formed hashes that simply match no rows, and the caller says "no spend
 * recorded". Answering "not found" would make a typo indistinguishable from an idle customer.
 */
export async function lookupAccountHashes(accountId: string): Promise<AccountLookup> {
  const trimmed = accountId.trim();
  if (!trimmed) return { ok: false, error: "Enter an account id to search for." };
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/account-lookup`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": await resolveTenantId() },
      body: JSON.stringify({ account_id: trimmed }),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) {
      // The gateway's own 422 messages are written to never quote the submitted value, so they are
      // safe to show. Anything else is reported as a status code without a body.
      const detail = res.status === 422 ? await readDetail(res) : null;
      return {
        ok: false,
        error: detail ?? `Account lookup failed (gateway HTTP ${res.status}).`,
      };
    }
    const body = (await res.json()) as AccountLookupResponse;
    const hashes = (body.hashes ?? [])
      .map((h) => h?.account_id_hash)
      .filter((h): h is string => typeof h === "string" && h.length > 0);
    if (hashes.length === 0 && body.account_id_hash) hashes.push(body.account_id_hash);
    if (hashes.length === 0) {
      return { ok: false, error: "Account lookup returned no hash for this tenant." };
    }
    return { ok: true, hashes };
  } catch (err) {
    // (err as Error).message here is a transport message: a timeout or a connection refusal. It
    // cannot contain the account id, which only ever left this process inside the request body.
    return { ok: false, error: `Account lookup unavailable: ${(err as Error).message}` };
  }
}

async function readDetail(res: Response): Promise<string | null> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : null;
  } catch {
    return null;
  }
}
