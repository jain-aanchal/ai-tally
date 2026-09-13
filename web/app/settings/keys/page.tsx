// SPDX-License-Identifier: Apache-2.0
// Ingest API keys settings page (Initiative 1, §7). Server component: it resolves the active tenant
// and the caller's role, then hands the client manager whether this user may mint/rotate/revoke.
// Members see the list read-only; admins get the write controls (§9).
import { defaultEndpoints } from "@/lib/connectSnippets";
import { canManage, getTenant } from "@/lib/getTenant";
import { KeyManager } from "./KeyManager";

export default async function KeysPage() {
  const tenant = await getTenant();
  // Resolved here, on the server, because TALLY_INGEST_URL is not visible to the browser.
  const endpoints = defaultEndpoints();
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">API Keys</h1>
        <p className="text-sm text-muted">
          Per-organization ingest keys for the SDK and the edge proxy. A key&apos;s secret is shown
          once at creation and never again.
        </p>
      </div>
      <KeyManager canManage={canManage(tenant)} endpoints={endpoints} />
    </div>
  );
}
