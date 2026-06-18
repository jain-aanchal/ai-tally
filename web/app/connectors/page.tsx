// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import { relativeAge } from "@/lib/dataState";
import {
  type ConnectorCategory,
  type ConnectorStatus,
  connectedCount,
} from "@/lib/connectors";
import { queryEnabledConnectors } from "@/lib/tenant";
import { queryStripeConfig, webhookUrl } from "@/lib/stripeConnector";
import type { IntegrationStatusRow } from "@/lib/clickhouse";
import { INTEGRATIONS, applyIntegrationStatus } from "@/lib/integrations";
import { ConnectorToggle } from "./ConnectorToggle";
import { IntegrationCards } from "./IntegrationCards";
import { StripeTile } from "./StripeTile";

interface ConnectorsPayload {
  connectors: ConnectorStatus[];
  live: boolean;
  // CTO-117: real per-tenant third-party integration status from the gateway. Empty array on a
  // fresh tenant — the UI renders every card as "Not connected" in that case.
  integrations: IntegrationStatusRow[];
  integrationsLive: boolean;
}

const SECTIONS: { category: ConnectorCategory; title: string; blurb: string }[] = [
  {
    category: "cost",
    title: "Cost sources",
    blurb: "All-in spend — beyond LLM tokens — attributed to features (CTO-63).",
  },
  {
    category: "revenue",
    title: "Revenue & CDP",
    blurb: "Value events that turn cost into ROI via attribution (CTO-68).",
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
}: {
  rows: ConnectorStatus[];
  enabledLayers: readonly string[];
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
                  {layer ? (
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
  const [{ connectors, live, integrations }, enabledLayers, stripeConfig] = await Promise.all([
    apiGet<ConnectorsPayload>("/api/connectors"),
    queryEnabledConnectors(),
    queryStripeConfig(),
  ]);
  const connected = connectedCount(connectors);
  // CTO-117: derive the three-state per-integration view from the gateway rows. A tenant with no
  // rows (fresh / nothing wired) gets a deck of "Not connected" cards — no fabricated stats.
  const integrationCards = applyIntegrationStatus(INTEGRATIONS, integrations ?? []);

  const body = (
    <div className="space-y-6">
      {SECTIONS.map((s) => {
        const rows = connectors.filter((c) => c.category === s.category);
        const n = connectedCount(rows);
        return (
          <Card key={s.category} title={`${s.title} — ${n}/${rows.length} connected`}>
            <p className="mb-3 max-w-prose text-xs text-muted">{s.blurb}</p>
            <ConnectorTable rows={rows} enabledLayers={enabledLayers} />
          </Card>
        );
      })}

      <Card title="Third-party integrations">
        <p className="mb-3 max-w-prose text-xs text-muted">
          Direct webhook / poller integrations. Each card shows the real per-tenant status from the
          gateway (CTO-117): healthy, failing, or not-connected. The Stripe webhook lights up its
          card automatically; Segment / HubSpot / Pendo light up as their workers land.
        </p>
        <IntegrationCards cards={integrationCards} />
        <div id="stripe-tile" className="mt-4 border-t border-edge pt-4">
          <p className="mb-2 text-xs text-muted">
            Stripe webhook setup — paste the signing secret from your Stripe Dashboard.
          </p>
          <StripeTile initialConfig={stripeConfig} webhookUrl={webhookUrl()} />
        </div>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Connectors</h1>
        <span className="text-sm text-muted">
          {connected} of {connectors.length} sources connected
        </span>
      </div>

      <p className="max-w-prose text-sm text-muted">
        Pluggable cost and revenue sources. Each normalizes one provider into the shared cost /
        business-event model; credentials and sync schedules are configured in the backend connector
        runner. A source shows <span className="text-good">Connected</span> once it has produced
        data.
      </p>

      {live ? body : <SyntheticPreviewBanner workflow="Connectors">{body}</SyntheticPreviewBanner>}
    </div>
  );
}
