// SPDX-License-Identifier: Apache-2.0
// Boot-time guard on the dev escape hatch (CTO-268).
//
// WHY this file exists. `TALLY_DEV_TENANT` is one environment variable that turns authentication
// OFF everywhere at once: `middleware.ts` exports a pass-through instead of `clerkMiddleware()`,
// `app/layout.tsx` mounts no `ClerkProvider`, `getTenant()` short-circuits to the pinned tenant
// instead of resolving the signed-in Clerk org, and `canManage()` returns true for every visitor,
// so key mint / rotate / revoke is ungated too. That is correct and wanted for `make up`, for CI,
// for the vitest suite and for the public demo VM. It is a catastrophe on a real instance: anyone
// with the URL reads every number in the system.
//
// The trap is concrete. `deploy/demo/` is the only genuine one-command deploy in the repo, so it is
// the thing an operator copies when standing up a real instance, and it exports `TALLY_DEV_TENANT`
// on purpose. The production manifests set it empty today, but nothing enforced that they stay that
// way. This module is that enforcement.
//
// THE CONTRACT. Disabling auth in a production build now takes TWO variables, the second of which
// nobody sets by accident:
//
//   TALLY_DEV_TENANT              pins the tenant (unchanged)
//   TALLY_ALLOW_INSECURE_NO_AUTH  "yes, I mean it, serve this with no authentication"
//
// One variable alone in a production build is a hard boot failure. Two is allowed and prints a
// standing warning on every boot, so the demo keeps working but stops being silent.
//
// Pure and dependency-free on purpose: it is imported from `instrumentation.ts` (Node runtime),
// from `middleware.ts` (Edge runtime) and from the vitest suite, so it must not import
// `server-only`, Clerk, or anything Node-specific.

/** Env keys this guard reads. Named once so the message and the checks cannot drift. */
export const DEV_TENANT_ENV = "TALLY_DEV_TENANT";
export const ALLOW_INSECURE_ENV = "TALLY_ALLOW_INSECURE_NO_AUTH";

/** The subset of `process.env` the guard needs. Passed explicitly so tests never mutate globals. */
export interface AuthGuardEnv {
  NODE_ENV?: string;
  NEXT_PHASE?: string;
  TALLY_DEV_TENANT?: string;
  TALLY_ALLOW_INSECURE_NO_AUTH?: string;
}

/** Raised when the escape hatch is active in a production build with no explicit opt-in. */
export class InsecureAuthConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InsecureAuthConfigError";
  }
}

/** Same truthiness spelling `deploy/demo/lib-tenant.sh` accepts, so shell and app agree. */
function isTruthy(v: string | undefined): boolean {
  if (!v) return false;
  return ["1", "true", "yes", "on"].includes(v.trim().toLowerCase());
}

function isSet(v: string | undefined): boolean {
  return typeof v === "string" && v.trim() !== "";
}

/**
 * What the guard decided, for callers that want to log rather than throw.
 *
 * - `ok`: authentication is on, or this is not a production build.
 * - `insecure-allowed`: auth is off and the operator opted in explicitly. Warn, loudly, forever.
 * - `refuse`: auth is off in production with no opt-in. `message` is the operator-facing text.
 */
export type AuthGuardVerdict =
  | { kind: "ok" }
  | { kind: "insecure-allowed"; message: string }
  | { kind: "refuse"; message: string };

function refusalMessage(devTenant: string): string {
  return `
================================================================================
ai-tally REFUSES TO START: ${DEV_TENANT_ENV} is set in a production build.

${DEV_TENANT_ENV} is the development escape hatch, and it disables authentication
COMPLETELY. With it set, the Clerk middleware becomes a pass-through, no
ClerkProvider is mounted, the tenant is pinned to

    ${devTenant}

instead of being resolved from a signed-in organization, and every visitor is
treated as an org admin who can mint, rotate and revoke API keys. Anyone who has
the URL sees every number in the system and can act on it.

There is no safe fallback here, so this process stops instead of serving.

You are in ONE OF TWO situations:

  1. You want a REAL, authenticated deployment. This is the normal case.

     Unset ${DEV_TENANT_ENV} (or set it to the empty string) and configure Clerk:

         NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
         CLERK_SECRET_KEY=sk_live_...

     The tenant then resolves from the signed-in Clerk organization, per
     web/lib/getTenant.ts.

     If you copied deploy/demo/, that export is the line to delete. The demo kit
     sets ${DEV_TENANT_ENV} deliberately and is NOT a template for a real
     instance: it serves synthetic data behind Caddy basic auth.

  2. You genuinely want an UNAUTHENTICATED instance, the way the public demo is.

     Then say so, explicitly, alongside ${DEV_TENANT_ENV}:

         ${ALLOW_INSECURE_ENV}=1

     Only do this behind access control you supply yourself, and only with
     synthetic data. deploy/demo/deploy.sh and deploy/demo/reseed.sh set it for
     you; you do not need to add it by hand there.

Checked: NODE_ENV=production, ${DEV_TENANT_ENV} set, ${ALLOW_INSECURE_ENV} not set.
================================================================================
`.trim();
}

function insecureWarning(devTenant: string): string {
  return `
================================================================================
ai-tally WARNING: serving with NO AUTHENTICATION.

${DEV_TENANT_ENV}=${devTenant} and ${ALLOW_INSECURE_ENV} is set, so this
production build starts with the dev escape hatch on: no sign-in, no Clerk org,
every visitor pinned to that tenant and treated as an org admin.

This is a deliberate configuration (the public demo runs this way behind basic
auth, with synthetic data). If you did not mean it, unset ${ALLOW_INSECURE_ENV}
and ${DEV_TENANT_ENV} and configure Clerk.
================================================================================
`.trim();
}

/**
 * Decide whether this process may serve, given its environment.
 *
 * Pure. `assertAuthConfig` is the one that actually stops the boot; this is separated out so tests
 * (and callers that want to log the allowed-but-insecure case) can inspect the decision.
 *
 * The production-BUILD phase is exempt. `next build` runs with NODE_ENV=production and CI builds
 * through the escape hatch on purpose (.github/workflows/ci.yml pins the same placeholder UUID the
 * vitest suite uses, because CI holds no Clerk keys). A build serves no requests; the artifact it
 * produces is checked when it boots, which is where the exposure would actually be.
 */
export function checkAuthConfig(env: AuthGuardEnv): AuthGuardVerdict {
  if (env.NEXT_PHASE === "phase-production-build") {
    return { kind: "ok" };
  }
  if (env.NODE_ENV !== "production") {
    return { kind: "ok" };
  }
  const devTenant = env.TALLY_DEV_TENANT;
  if (!isSet(devTenant)) {
    return { kind: "ok" };
  }
  const pinned = (devTenant as string).trim();
  if (isTruthy(env.TALLY_ALLOW_INSECURE_NO_AUTH)) {
    return { kind: "insecure-allowed", message: insecureWarning(pinned) };
  }
  return { kind: "refuse", message: refusalMessage(pinned) };
}

/**
 * Throw {@link InsecureAuthConfigError} when the escape hatch is active in a production build with
 * no explicit opt-in, and warn on stderr when it is active WITH one.
 *
 * Call sites are boot-time on purpose (see `instrumentation.ts`). A per-request check would be
 * skippable by any route that forgets to call it.
 */
export function assertAuthConfig(env: AuthGuardEnv = process.env as AuthGuardEnv): void {
  const verdict = checkAuthConfig(env);
  if (verdict.kind === "refuse") {
    throw new InsecureAuthConfigError(verdict.message);
  }
  if (verdict.kind === "insecure-allowed") {
    console.warn(verdict.message);
  }
}
