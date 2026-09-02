// SPDX-License-Identifier: Apache-2.0
// Member management (Initiative 1, §7/§9). ai-tally does not build its own membership UI: Clerk's
// OrganizationProfile owns invites, role changes, and removals. Clerk itself gates the write actions
// to admins, matching the role model in §9.
import { OrganizationProfile } from "@clerk/nextjs";

import { devTenant } from "@/lib/getTenant";

// Clerk's OrganizationProfile is a client component that requires a ClerkProvider (and a
// publishableKey). On the dev escape hatch (TALLY_DEV_TENANT set) the layout mounts no ClerkProvider,
// exactly as the shell omits the org switcher, so rendering it there would throw. Gate the same way
// the shell does and show why instead. `force-dynamic` keeps the product path off the keyless build
// prerender, so the page resolves Clerk at request time rather than at build (CTO-259).
export const dynamic = "force-dynamic";

export default function MembersPage() {
  if (devTenant() !== null) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold">Members</h1>
        <p className="text-sm text-muted">
          Member management runs through Clerk, which is disabled in the dev escape hatch
          (TALLY_DEV_TENANT is set). Run with Clerk configured to invite members and manage roles.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Members</h1>
      <OrganizationProfile routing="hash" />
    </div>
  );
}
