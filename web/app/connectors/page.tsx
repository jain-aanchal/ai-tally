// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import {
  type ConnectorCategory,
  type ConnectorStatus,
  comingSoonCount,
  connectedCount,
  liveAvailableCount,
} from "@/lib/connectors";
import { queryCostConnectorConfigs } from "@/lib/costConnectors";
import { queryEnabledConnectors } from "@/lib/tenant";
import { ConnectorTable } from "./ConnectorTable";

interface ConnectorsPayload {
  connectors: ConnectorStatus[];
  live: boolean;
}

const SECTIONS: { category: ConnectorCategory; title: string; blurb: string }[] = [
  {
    category: "cost",
    title: "Cost sources",
    blurb: "All-in spend — beyond LLM tokens — attributed to features (CTO-63).",
  },
];

export default async function ConnectorsPage() {
  const [{ connectors, live }, enabledLayers, costConfigs] = await Promise.all([
    apiGet<ConnectorsPayload>("/api/connectors"),
    queryEnabledConnectors(),
    queryCostConnectorConfigs(),
  ]);
  // `null` means the gateway is unreachable. Render the forms anyway (they report their own
  // errors on submit) rather than hiding the only way to configure anything.
  const configs = costConfigs ?? [];
  // Only cost-layer sources surface in the UI today; revenue/CDP and the third-party integration
  // cards were purely decorative (no real status), removed in the cleanup wave following #100.
  const visibleConnectors = connectors.filter((c) => c.category === "cost");
  const connected = connectedCount(visibleConnectors);

  const body = (
    <div className="space-y-6">
      {SECTIONS.map((s) => {
        const rows = visibleConnectors.filter((c) => c.category === s.category);
        const n = connectedCount(rows);
        const live = liveAvailableCount(rows);
        const soon = comingSoonCount(rows);
        const suffix = soon > 0 ? ` · ${soon} coming soon` : "";
        return (
          <Card key={s.category} title={`${s.title} — ${n}/${live} connected${suffix}`}>
            <p className="mb-3 max-w-prose text-xs text-muted">{s.blurb}</p>
            <ConnectorTable rows={rows} enabledLayers={enabledLayers} configs={configs} />
          </Card>
        );
      })}
    </div>
  );

  const totalSoon = comingSoonCount(visibleConnectors);
  const totalLive = liveAvailableCount(visibleConnectors);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Connectors</h1>
        <span className="text-sm text-muted">
          {connected} of {totalLive} sources connected
          {totalSoon > 0 ? ` · ${totalSoon} coming soon` : ""}
        </span>
      </div>

      <p className="max-w-prose text-sm text-muted">
        Pluggable cost sources. Each normalizes one provider into the shared cost model. Connect a
        source here by pointing it at a credential reference; sync schedules run in the backend
        connector runner. A source shows <span className="text-good">Connected</span> once it has
        produced data.
      </p>

      {live ? body : <SyntheticPreviewBanner workflow="Connectors">{body}</SyntheticPreviewBanner>}
    </div>
  );
}
