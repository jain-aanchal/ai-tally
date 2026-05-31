// Connectors workflow (CTO-63 cost sources + CTO-68 revenue/CDP sources).
//
// The backend ships pluggable connector frameworks (tally.cost_connectors, tally.cdp_connectors):
// each connector normalizes one provider's payload into the shared cost/business-event model. This
// module is the UI's view of that catalog — the supported sources and whether each is currently
// producing data — so the page reflects the real frameworks rather than inventing providers.
//
// "Connected" is derived honestly from telemetry: a cost source counts as connected when its cost
// layer has rows in otel_spans; a revenue source when business_events carries its Source. Nothing
// is fetched here — configuration/credentials live in the backend runner, not the dashboard.

import type { Layer } from "./cost";

export type ConnectorCategory = "cost" | "revenue";

/** How a connector's live activity is found in the telemetry store. */
export type LiveKey =
  | { kind: "cost-layer"; layer: Layer }
  | { kind: "revenue-source"; source: string };

export interface ConnectorDef {
  /** Stable id — matches the backend connector's `name`. */
  id: string;
  name: string;
  category: ConnectorCategory;
  /** Short label for what this source feeds (e.g. "Vector DB cost", "Revenue events"). */
  feeds: string;
  description: string;
  liveKey: LiveKey;
}

export type ConnectorState = "connected" | "available";

export interface ConnectorStatus extends ConnectorDef {
  state: ConnectorState;
  /** Records ingested from this source in the trailing window (0 when not connected). */
  records: number;
  /** ISO timestamp of the most recent record, or null when never synced. */
  lastAt: string | null;
}

/** The supported-connector catalog. Mirrors tally.cost_connectors / tally.cdp_connectors. */
export const CONNECTORS: ConnectorDef[] = [
  {
    id: "llm_proxy",
    name: "LLM proxy / SDK",
    category: "cost",
    feeds: "LLM cost",
    description: "Token usage from the edge proxy or SDK — the primary spend signal.",
    liveKey: { kind: "cost-layer", layer: "llm" },
  },
  {
    id: "pinecone",
    name: "Pinecone",
    category: "cost",
    feeds: "Vector DB cost",
    description: "Vector database read/write/storage usage attributed to features.",
    liveKey: { kind: "cost-layer", layer: "vector" },
  },
  {
    id: "tavily",
    name: "Tavily",
    category: "cost",
    feeds: "Tool-call cost",
    description: "Search / tool API usage billed per call.",
    liveKey: { kind: "cost-layer", layer: "tools" },
  },
  {
    id: "aws_cost_explorer",
    name: "AWS Cost Explorer",
    category: "cost",
    feeds: "Compute cost",
    description: "Cloud billing line items for compute backing the AI workload.",
    liveKey: { kind: "cost-layer", layer: "compute" },
  },
  {
    id: "vercel",
    name: "Vercel",
    category: "cost",
    feeds: "Egress cost",
    description: "Edge/serverless and bandwidth usage for the serving tier.",
    liveKey: { kind: "cost-layer", layer: "egress" },
  },
  {
    id: "segment",
    name: "Segment",
    category: "revenue",
    feeds: "Revenue events",
    description: "CDP track events — the value side of ROI attribution.",
    liveKey: { kind: "revenue-source", source: "segment" },
  },
  {
    id: "rudderstack",
    name: "RudderStack",
    category: "revenue",
    feeds: "Revenue events",
    description: "Open-source CDP track events (Segment-compatible payloads).",
    liveKey: { kind: "revenue-source", source: "rudderstack" },
  },
  {
    id: "stripe",
    name: "Stripe",
    category: "revenue",
    feeds: "Monetary events",
    description: "Payments, subscriptions and refunds as monetary business events.",
    liveKey: { kind: "revenue-source", source: "stripe" },
  },
  {
    id: "hubspot",
    name: "HubSpot",
    category: "revenue",
    feeds: "Lifecycle events",
    description: "Deal-stage / lifecycle changes (e.g. closed-won) for attribution.",
    liveKey: { kind: "revenue-source", source: "hubspot" },
  },
];

/** Per-connector activity pulled from the telemetry store, keyed by connector id. */
export interface ConnectorActivity {
  records: Record<string, number>;
  lastAt: Record<string, string>;
}

/**
 * Merge the static catalog with observed activity into display rows. A source is "connected" when
 * it has produced at least one record; otherwise it's "available" (supported, not yet wired). Pure
 * and deterministic so it can be unit-tested without a database.
 */
export function applyActivity(
  catalog: ConnectorDef[],
  activity: ConnectorActivity,
): ConnectorStatus[] {
  return catalog.map((def) => {
    const records = activity.records[def.id] ?? 0;
    const lastAt = activity.lastAt[def.id] ?? null;
    return {
      ...def,
      records,
      lastAt,
      state: records > 0 ? "connected" : "available",
    };
  });
}

export function connectedCount(rows: ConnectorStatus[]): number {
  return rows.filter((r) => r.state === "connected").length;
}

/** Mock activity for when ClickHouse is unreachable — shows a populated, plausible example. */
export const mockActivity: ConnectorActivity = {
  records: { llm_proxy: 4120, stripe: 86 },
  lastAt: {
    llm_proxy: "2026-05-31T18:40:00Z",
    stripe: "2026-05-31T17:05:00Z",
  },
};

export const mockConnectorStatuses: ConnectorStatus[] = applyActivity(CONNECTORS, mockActivity);
