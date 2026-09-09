// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { Blank } from "@/components/HonestValue";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import {
  type ConnectorCategory,
  type ConnectorStatus,
  comingSoonCount,
  connectedCount,
  liveAvailableCount,
} from "@/lib/connectors";
import { queryCostConnectorConfigs } from "@/lib/costConnectors";
import { queryRevenueUploads } from "@/lib/revenueUpload";
import type { SourceState } from "@/lib/dataState";
import { queryEnabledConnectors } from "@/lib/tenant";
import { ConnectorTable } from "./ConnectorTable";
import { RevenueUpload } from "./RevenueUpload";

interface ConnectorsPayload {
  connectors: ConnectorStatus[];
  live: boolean;
  /** Which of the four source states produced the per-connector record counts (#363). */
  activity: SourceState;
}

const SECTIONS: { category: ConnectorCategory; title: string; blurb: string }[] = [
  {
    category: "cost",
    title: "Cost sources",
    blurb: "All-in spend beyond LLM tokens, attributed to features (CTO-63).",
  },
];

export default async function ConnectorsPage() {
  const [{ connectors, activity }, enabledLayers, costConfigs, revenueUploads] = await Promise.all([
    apiGet<ConnectorsPayload>("/api/connectors"),
    queryEnabledConnectors(),
    queryCostConnectorConfigs(),
    queryRevenueUploads(),
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
          <Card key={s.category} title={`${s.title}: ${n}/${live} connected${suffix}`}>
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
      <PageHeader
        title="Connectors"
        subtitle={
          <span className="block max-w-prose">
            Pluggable cost sources. Each normalizes one provider into the shared cost model. Connect
            a source here by pointing it at a credential reference; sync schedules run in the backend
            connector runner. A source shows <span className="text-good">Connected</span> once it
            has produced data.
          </span>
        }
        actions={
          <span className="rounded-full border border-edge bg-panel px-3 py-1 text-sm text-muted">
            {connected} of {totalLive} sources connected
            {totalSoon > 0 ? ` · ${totalSoon} coming soon` : ""}
          </span>
        }
      />

      {/* #363. The activity read decides how the catalog is captioned, and only a demo build ever
          gets the fixture's 4,120 llm_proxy records. An unreadable source says so above the rows
          rather than dressing "Not connected" up as a measurement; a source that answered with no
          records IS the measurement, and every row correctly reads "Not connected". */}
      {activity === "unavailable" && (
        <p className="rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-sm text-warn">
          <Blank reason="the telemetry store could not be read, so no record counts were returned" />{" "}
          Per-source record counts could not be read. The rows below show the catalog and its
          configuration; their activity is unknown, not zero.
        </p>
      )}
      {activity === "sample" ? (
        <SyntheticPreviewBanner workflow="Connectors">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}

      {/*
        Revenue is the other half of margin, and for plenty of B2B companies it has no API worth
        polling. This sits outside the sample-data wrapper above on purpose: the upload is a real
        write against the real control plane whether or not any telemetry has arrived yet, and
        wrapping it in a "sample data" frame would suggest otherwise.
      */}
      <Card title="Revenue upload: CSV">
        <RevenueUpload snapshots={revenueUploads ?? []} unreachable={revenueUploads === null} />
      </Card>
    </div>
  );
}
