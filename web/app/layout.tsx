// SPDX-License-Identifier: Apache-2.0
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { ClerkProvider } from "@clerk/nextjs";
import { Shell } from "@/components/Shell";
import { devTenant } from "@/lib/getTenant";
import "./globals.css";

export const metadata: Metadata = {
  title: "ai-tally",
  description: "Cost-and-value observability for AI products",
};

// Dev escape hatch (Initiative 1, §10): with TALLY_DEV_TENANT set the app renders with NO Clerk keys
// and no login, so `make up` and CI work with no Clerk account. In that mode we skip ClerkProvider
// entirely (mounting it with no keys would throw) and hide the org controls in the shell. On the
// product path ClerkProvider wraps the tree and the shell shows the org switcher.
export default function RootLayout({ children }: { children: ReactNode }) {
  const devTenantId = devTenant();
  const dev = devTenantId !== null;
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
          {children}
        </Shell>
      </body>
    </html>
  );

  if (dev) {
    return body;
  }
  return <ClerkProvider>{body}</ClerkProvider>;
}
