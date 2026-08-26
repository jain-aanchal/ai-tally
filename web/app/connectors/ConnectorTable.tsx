// SPDX-License-Identifier: Apache-2.0
"use client";

// The /connectors source table, on the shared `DataTable` (CTO-179).
//
// This lives in its own client module rather than inside page.tsx because `DataTable` takes a
// column spec whose `render` entries are functions, and functions cannot cross the server/client
// boundary. page.tsx is an async server component that fetches the catalog, so the column spec has
// to be declared on the client side of the line. Every page migrating to `DataTable` will hit this,
// which is the cost of a render-prop API and cheaper than the alternatives (a serializable cell
// descriptor language, or a server table that cannot sort or page).

import { useMemo } from "react";

import { DataTable, type Column } from "@/components/DataTable";
import { Blank } from "@/components/HonestValue";
import type { ConnectorStatus } from "@/lib/connectors";
import { relativeAge } from "@/lib/dataState";
import { type CostConnectorConfig, isConfigurable } from "@/lib/costConnectors";
import { ConnectForm } from "./ConnectForm";
import { ConnectorToggle } from "./ConnectorToggle";

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

/**
 * Only cost-layer connectors participate in the per-tenant declaration; revenue connectors have
 * their own connectivity story that doesn't drive the layer banner.
 */
function layerOf(row: ConnectorStatus): string | null {
  return row.liveKey.kind === "cost-layer" ? row.liveKey.layer : null;
}

export function ConnectorTable({
  rows,
  enabledLayers,
  configs,
}: {
  rows: readonly ConnectorStatus[];
  enabledLayers: readonly string[];
  /**
   * Flat list rather than the Map the page used to build. The page is a server component, so this
   * crosses the serialization boundary and a plain array is the least surprising thing to send.
   */
  configs: readonly CostConnectorConfig[];
}) {
  const configByConnector = useMemo(
    () => new Map(configs.map((c) => [c.connector, c])),
    [configs],
  );

  const columns = useMemo<Column<ConnectorStatus>[]>(
    () => [
      {
        key: "source",
        header: "Source",
        render: (r) => (
          <>
            <div className="font-medium">{r.name}</div>
            <div className="max-w-prose text-xs text-muted">{r.description}</div>
          </>
        ),
      },
      {
        key: "feeds",
        header: "Feeds",
        cellClassName: "text-muted",
        render: (r) => r.feeds,
      },
      {
        key: "status",
        header: "Status",
        render: (r) => <StateBadge row={r} />,
      },
      {
        key: "records",
        header: "Records (30d)",
        align: "right",
        render: (r) =>
          r.records > 0 ? (
            r.records.toLocaleString()
          ) : (
            // Zero records is exactly how "connected" is decided, so an empty count is never a
            // real zero: the source has not delivered anything we can count yet.
            <Blank reason="this source has not delivered any records in the last 30 days" />
          ),
      },
      {
        key: "lastSync",
        header: "Last sync",
        align: "right",
        cellClassName: "text-muted",
        render: (r) =>
          r.lastAt ? (
            relativeAge(r.lastAt)
          ) : (
            <Blank reason="this source has never synced, so there is no last-sync time" />
          ),
      },
      {
        key: "connection",
        header: "Connection",
        align: "right",
        render: (r) =>
          isConfigurable(r.id) ? (
            <ConnectForm
              connector={r.id}
              configured={configByConnector.get(r.id)?.configured ?? false}
              credentialsRef={configByConnector.get(r.id)?.credentialsRef ?? null}
              details={configByConnector.get(r.id)?.details ?? {}}
            />
          ) : (
            <Blank
              className="text-xs"
              reason="this source takes no credentials from the dashboard; it is configured where it is deployed"
            />
          ),
      },
      {
        key: "banner",
        header: "Banner",
        align: "right",
        render: (r) => {
          const layer = layerOf(r);
          return layer && r.state !== "coming_soon" ? (
            <ConnectorToggle layer={layer} initialEnabled={enabledLayers.includes(layer)} />
          ) : (
            <Blank
              className="text-xs"
              reason="this source is not ingesting into a cost layer yet, so there is nothing for the partial-data banner to count"
            />
          );
        },
      },
    ],
    [configByConnector, enabledLayers],
  );

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => r.id}
      // The catalog is a fixed, short list shipped in lib/connectors, so paging it would add a
      // pager that never has a second page to show.
      pageSize={0}
      rowClassName={() => "align-top"}
    />
  );
}
