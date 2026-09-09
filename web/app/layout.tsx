// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { ClerkProvider } from "@clerk/nextjs";
import { Shell } from "@/components/Shell";
import { TenantNotProvisionedError, devTenant, getTenant } from "@/lib/getTenant";
import { WorkspaceProvisioning } from "./WorkspaceProvisioning";
import "./globals.css";

export const metadata: Metadata = {
  title: "ai-tally",
  description: "Cost-and-value observability for AI products",
};

// Dev escape hatch (Initiative 1, §10): with TALLY_DEV_TENANT set the app renders with NO Clerk keys
// and no login, so `make up` and CI work with no Clerk account. In that mode we skip ClerkProvider
// entirely (mounting it with no keys would throw) and hide the org controls in the shell. On the
// product path ClerkProvider wraps the tree and the shell shows the org switcher.
/**
 * Is this request's org still waiting for its tenant row (#358)?
 *
 * The check lives in the layout because the race can hit ANY route (Home, onboarding, a bookmarked
 * deep link), and one place that knows about it beats a try/catch on every page.
 *
 * It is deliberately narrow. Only {@link TenantNotProvisionedError}, the deliberate 404 from
 * `by-clerk-org`, is handled here, because only that one is transient and recoverable. Everything
 * else, including "no active org" (the middleware already redirects those to /select-org) and a
 * gateway that is answering 503, is left exactly as it is: the page resolves the tenant too, and
 * its throw lands in `app/error.tsx` with the shell intact. So this gate can only ADD a recovery,
 * never swallow a failure.
 */
async function isAwaitingProvisioning(): Promise<boolean> {
  try {
    await getTenant();
    return false;
  } catch (err) {
    return err instanceof TenantNotProvisionedError;
  }
}

export default async function RootLayout({ children }: { children: ReactNode }) {
  const devTenantId = devTenant();
  const dev = devTenantId !== null;
  // No Clerk, no webhook, no race: the escape hatch resolves its pinned tenant synchronously.
  const awaitingProvisioning = dev ? false : await isAwaitingProvisioning();
  // In the dev escape hatch there is no Clerk org to read, so the shell names the tenant. Let a demo
  // or local deployment show a friendly organization name instead of the raw tenant id via
  // TALLY_DEV_ORG_NAME (CTO-262); falls back to the tenant value when unset.
  const devLabel = process.env.TALLY_DEV_ORG_NAME ?? devTenantId;

  const body = (
    <html lang="en">
      <body>
        {/* On the dev escape hatch the shell names the pinned tenant (no Clerk org exists to read);
            on the product path it reads the active org name from Clerk (Initiative 1, §7/§10). */}
        <Shell showOrgControls={!dev} devTenantLabel={devLabel}>
          {awaitingProvisioning ? <WorkspaceProvisioning /> : children}
        </Shell>
      </body>
    </html>
  );

  if (dev) {
    return body;
  }
  return <ClerkProvider>{body}</ClerkProvider>;
}
