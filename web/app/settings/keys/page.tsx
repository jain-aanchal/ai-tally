// SPDX-License-Identifier: Apache-2.0
// Ingest API keys settings page (Initiative 1, §7). Server component: it resolves the active tenant
// and the caller's role, then hands the client manager whether this user may mint/rotate/revoke.
// Members see the list read-only; admins get the write controls (§9).
import { defaultEndpoints } from "@/lib/connectSnippets";
import { canManage, getTenant } from "@/lib/getTenant";
import { queryProxyEnabled } from "@/lib/proxySetting";
import { KeyManager } from "./KeyManager";
import { ProxySwitch } from "./ProxySwitch";

export default async function KeysPage() {
  const tenant = await getTenant();
  // Resolved here, on the server, because TALLY_INGEST_URL is not visible to the browser.
  const endpoints = defaultEndpoints();
  // Review of #385, finding 3: the switch exists only where a hosted proxy does. On a deployment
  // without one, "Turn on" would report that proxies start accepting this org's keys when there is no
  // proxy at all, so neither the setting is read nor the switch rendered.
  const proxyEnabled = endpoints.proxyDeployed ? await queryProxyEnabled(tenant.tenantId) : null;
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">API Keys</h1>
        <p className="text-sm text-muted">
          Per-organization ingest keys for the SDK and the edge proxy. A key&apos;s secret is shown
          once at creation and never again.
        </p>
      </div>
      {endpoints.proxyDeployed && (
        <ProxySwitch initialEnabled={proxyEnabled} canManage={canManage(tenant)} />
      )}
      <KeyManager canManage={canManage(tenant)} endpoints={endpoints} proxyEnabled={proxyEnabled} />
    </div>
  );
}
