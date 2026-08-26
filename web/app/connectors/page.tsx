// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import { relativeAge } from "@/lib/dataState";
import {
  type ConnectorCategory,
  type ConnectorStatus,
  comingSoonCount,
  connectedCount,
  liveAvailableCount,
} from "@/lib/connectors";
import { type CostConnectorConfig, isConfigurable, queryCostConnectorConfigs } from "@/lib/costConnectors";
import { queryRevenueUploads } from "@/lib/revenueUpload";
import { queryEnabledConnectors } from "@/lib/tenant";
import { ConnectForm } from "./ConnectForm";
import { ConnectorToggle } from "./ConnectorToggle";
import { RevenueUpload } from "./RevenueUpload";

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

function StateBadge({ row }: { row: ConnectorStatus }) {
  if (row.state === "connected") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-good/40 bg-good/10 px-2 py-0.5 text-xs font-medium text-good">
        <span className="h-1.5 w-1.5 rounded-full bg-good" />
        Connected
      </span>
    );
  }
  if (row.state === "coming_soon") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-ink/40 px-2 py-0.5 text-xs font-medium text-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-muted" />
        Coming soon
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-edge bg-ink/40 px-2 py-0.5 text-xs font-medium text-muted">
      <span className="h-1.5 w-1.5 rounded-full bg-muted" />
      Available
    </span>
  );
}

function ConnectorTable({
  rows,
  enabledLayers,
  configs,
}: {
  rows: ConnectorStatus[];
  enabledLayers: readonly string[];
  configs: Map<string, CostConnectorConfig>;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-muted">
          <tr>
            <th className="py-1 text-left font-medium">Source</th>
            <th className="py-1 text-left font-medium">Feeds</th>
            <th className="py-1 text-left font-medium">Status</th>
            <th className="py-1 text-right font-medium">Records (30d)</th>
            <th className="py-1 text-right font-medium">Last sync</th>
            <th className="py-1 text-right font-medium">Connection</th>
            <th className="py-1 text-right font-medium">Banner</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            // Only cost-layer connectors participate in the per-tenant declaration; revenue
            // connectors have their own connectivity story that doesn't drive the layer banner.
            const layer = r.liveKey.kind === "cost-layer" ? r.liveKey.layer : null;
            const isEnabled = layer ? enabledLayers.includes(layer) : false;
            return (
              <tr key={r.id} className="border-t border-edge align-top">
                <td className="py-2">
                  <div className="font-medium">{r.name}</div>
                  <div className="max-w-prose text-xs text-muted">{r.description}</div>
                </td>
                <td className="py-2 text-muted">{r.feeds}</td>
                <td className="py-2">
                  <StateBadge row={r} />
                </td>
                <td className="py-2 text-right tabular-nums">
                  {r.records > 0 ? r.records.toLocaleString() : "—"}
                </td>
                <td className="py-2 text-right tabular-nums text-muted">
                  {r.lastAt ? relativeAge(r.lastAt) : "—"}
                </td>
                <td className="py-2 text-right">
                  {isConfigurable(r.id) ? (
                    <ConnectForm
                      connector={r.id}
                      configured={configs.get(r.id)?.configured ?? false}
                      credentialsRef={configs.get(r.id)?.credentialsRef ?? null}
                      details={configs.get(r.id)?.details ?? {}}
                    />
                  ) : (
                    <span className="text-xs text-muted">—</span>
                  )}
                </td>
                <td className="py-2 text-right">
                  {layer && r.state !== "coming_soon" ? (
                    <ConnectorToggle layer={layer} initialEnabled={isEnabled} />
                  ) : (
                    <span className="text-xs text-muted">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default async function ConnectorsPage() {
  const [{ connectors, live }, enabledLayers, costConfigs, revenueUploads] = await Promise.all([
    apiGet<ConnectorsPayload>("/api/connectors"),
    queryEnabledConnectors(),
    queryCostConnectorConfigs(),
    queryRevenueUploads(),
  ]);
  // `null` means the gateway is unreachable. Render the forms anyway (they report their own
  // errors on submit) rather than hiding the only way to configure anything.
  const configs = new Map((costConfigs ?? []).map((c) => [c.connector, c]));
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

      {/*
        Revenue is the other half of margin, and for plenty of B2B companies it has no API worth
        polling. This sits outside the sample-data wrapper above on purpose: the upload is a real
        write against the real control plane whether or not any telemetry has arrived yet, and
        wrapping it in a "sample data" frame would suggest otherwise.
      */}
      <Card title="Revenue upload — CSV">
        <RevenueUpload snapshots={revenueUploads ?? []} unreachable={revenueUploads === null} />
      </Card>
    </div>
  );
}
