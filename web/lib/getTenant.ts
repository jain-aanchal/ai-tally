// SPDX-License-Identifier: Apache-2.0
// Resolve the active Clerk organization to its ai-tally tenant UUID (Initiative 1, §7/§10).
//
// This is the single seam between Clerk identity and ai-tally's tenant scoping. Every server read
// and every control-plane write asks it "which tenant am I acting for" instead of reading a
// module-level `TALLY_TENANT_ID ?? "local-dev"` constant, so `local-dev` no longer lives on the
// product path.
//
// TWO MODES.
//
//  * Product path. Read `{ orgId, orgRole }` from Clerk `auth()`, resolve the org to a tenant UUID
//    via the gateway's service-token-authed `GET /v1/tenant/by-clerk-org/{orgId}` (cached per org
//    with a short TTL so it is not a per-request round-trip), and return the UUID. When there is no
//    active org we THROW rather than fall back: the product has no personal workspace, and a silent
//    `local-dev` would leak one tenant's data onto another's screen.
//
//  * Dev escape hatch (§10). When `TALLY_DEV_TENANT` is set, `getTenant()` short-circuits and Clerk
//    is never consulted, so `make up` and CI run with no Clerk account and no Clerk keys. This is
//    the ONLY place a pinned tenant (including the name `local-dev`) survives, and it is opt-in.
//
// Server-only. Imported by Route Handlers and server components; Clerk is loaded lazily so this
// module (and the dev path) never pulls Clerk into a context that has no keys, including the vitest
// suite, which sets `TALLY_DEV_TENANT` so the short-circuit is taken.

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** Short TTL so switching orgs re-scopes within a minute without a per-request gateway round-trip. */
const CACHE_TTL_MS = 60_000;

export interface ResolvedTenant {
  /** The canonical `tenants.id` UUID that scopes every read and write. */
  tenantId: string;
  /** The active Clerk org id, or null on the dev escape hatch. */
  orgId: string | null;
  /** The caller's Clerk role in the active org (`org:admin` / `org:member`), or null in dev. */
  orgRole: string | null;
}

/** No active organization on the Clerk session. Callers redirect to select-or-create-org (§7). */
export class NoActiveOrgError extends Error {
  constructor() {
    super("no active organization");
    this.name = "NoActiveOrgError";
  }
}

/**
 * The pinned dev tenant, or null when the escape hatch is off.
 *
 * A UUID or the name `local-dev`. When set, the product skips Clerk entirely. When unset, the
 * product path refuses to serve tenant data without a resolved org, by design.
 */
export function devTenant(): string | null {
  const v = process.env.TALLY_DEV_TENANT;
  return v && v.trim() ? v.trim() : null;
}

/**
 * The control-plane service-token header (§6). The web server is the only legitimate caller of the
 * gateway control plane and authenticates with a server-only shared secret. Empty when unset (local
 * dev with auth off), so `make up` is unaffected. The token never reaches a client bundle.
 */
export function serviceTokenHeader(): Record<string, string> {
  const token = process.env.GATEWAY_SERVICE_TOKEN;
  return token ? { authorization: `Bearer ${token}` } : {};
}

/**
 * Headers for a control-plane call: the resolved tenant UUID plus the service token (when set).
 * `extra` merges in per-call headers such as `content-type`.
 */
export function controlPlaneHeaders(
  tenantId: string,
  extra?: Record<string, string>,
): Record<string, string> {
  return { "x-tenant-id": tenantId, ...serviceTokenHeader(), ...(extra ?? {}) };
}

const _orgCache = new Map<string, { tenantId: string; plan: string; at: number }>();

async function resolveOrgToTenant(orgId: string): Promise<{ tenantId: string; plan: string }> {
  const cached = _orgCache.get(orgId);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
    return { tenantId: cached.tenantId, plan: cached.plan };
  }
  const res = await fetch(
    `${GATEWAY_URL}/v1/tenant/by-clerk-org/${encodeURIComponent(orgId)}`,
    { headers: serviceTokenHeader(), cache: "no-store", signal: AbortSignal.timeout(2000) },
  );
  if (!res.ok) {
    throw new Error(`by-clerk-org resolve failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as { tenant_id: string; plan: string };
  const resolved = { tenantId: body.tenant_id, plan: body.plan };
  _orgCache.set(orgId, { ...resolved, at: Date.now() });
  return resolved;
}

/**
 * Resolve the caller's active tenant. See the module docstring for the two modes.
 *
 * Throws {@link NoActiveOrgError} on the product path when there is no active org, and never falls
 * back to `local-dev` unless the dev escape hatch is set.
 */
export async function getTenant(): Promise<ResolvedTenant> {
  const dev = devTenant();
  if (dev) {
    return { tenantId: dev, orgId: null, orgRole: null };
  }
  const { auth } = await import("@clerk/nextjs/server");
  const { orgId, orgRole } = await auth();
  if (!orgId) {
    throw new NoActiveOrgError();
  }
  const { tenantId } = await resolveOrgToTenant(orgId);
  return { tenantId, orgId, orgRole: orgRole ?? null };
}

/** Convenience: just the tenant UUID. The common case for a read or a control-plane call. */
export async function resolveTenantId(): Promise<string> {
  return (await getTenant()).tenantId;
}
