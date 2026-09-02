// SPDX-License-Identifier: Apache-2.0
// Member management (Initiative 1, §7/§9). ai-tally does not build its own membership UI: Clerk's
// OrganizationProfile owns invites, role changes, and removals. Clerk itself gates the write actions
// to admins, matching the role model in §9.
import { OrganizationProfile } from "@clerk/nextjs";

export default function MembersPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Members</h1>
      <OrganizationProfile routing="hash" />
    </div>
  );
}
