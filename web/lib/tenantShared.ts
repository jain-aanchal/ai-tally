// SPDX-License-Identifier: Apache-2.0
// Client-safe tenant constants and types (Initiative 1, §9; CTO-259).
//
// `getTenant.ts` is server-only: it imports `server-only` and lazily loads Clerk, so any client
// module that reaches it breaks the build. The symbols here carry no server dependency (no Clerk, no
// gateway fetch, no `server-only`) and are the pieces a Client Component may legitimately need. They
// live in this separate module so a client can import them without dragging the server graph in;
// `getTenant.ts` re-exports them so server callers keep a single import site.

/**
 * The active Clerk organization resolved to its ai-tally tenant, as returned by the server-only
 * `getTenant()`. Declared here (not in `getTenant.ts`) so a Client Component that only needs the
 * shape can type against it without importing the server module.
 */
export interface ResolvedTenant {
  /** The canonical `tenants.id` UUID that scopes every read and write. */
  tenantId: string;
  /** The active Clerk org id, or null on the dev escape hatch. */
  orgId: string | null;
  /** The caller's Clerk role in the active org (`org:admin` / `org:member`), or null in dev. */
  orgRole: string | null;
}

/** Clerk's admin role in an org. Key and member writes require it (§9). */
export const ORG_ADMIN_ROLE = "org:admin";
