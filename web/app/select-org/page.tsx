// SPDX-License-Identifier: Apache-2.0
// Select-or-create-organization screen (Initiative 1, §7). A signed-in user with no active org is
// redirected here by the middleware. Creating an org fires `organization.created`, which provisions
// the tenant behind the scenes (§4); picking an existing org sets it active. The product has no
// personal workspace, so both paths lead into the dashboard scoped to the chosen org.
import { OrganizationList } from "@clerk/nextjs";

// Clerk's OrganizationList needs a ClerkProvider and a publishableKey. Rendering per request keeps
// this off the keyless build prerender (dev escape hatch / CI have no Clerk keys) and resolves Clerk
// at request time on the product path (CTO-259). The dev middleware never routes here.
export const dynamic = "force-dynamic";

export default function SelectOrgPage() {
  return (
    <div className="flex min-h-[70vh] flex-col items-center justify-center gap-6">
      <div className="text-center">
        <h1 className="text-lg font-semibold">Choose an organization</h1>
        <p className="text-sm text-muted">
          Create one to get started, or pick an organization you already belong to.
        </p>
      </div>
      <OrganizationList
        hidePersonal
        afterSelectOrganizationUrl="/"
        afterCreateOrganizationUrl="/"
      />
    </div>
  );
}
