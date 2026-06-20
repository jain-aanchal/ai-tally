// SPDX-License-Identifier: Apache-2.0
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

/**
 * Whether the backend ingest path for this connector actually exists today.
 * - `live`        — a real worker / SDK / webhook ingests data when configured (LLM proxy, Stripe)
 * - `coming_soon` — catalog entry only; no worker yet, the UI advertises a placeholder so users
 *                   know it's planned. Re-classify to `live` when the corresponding integration
 *                   ships (Pinecone, Tavily, AWS Cost Explorer, Vercel via CTO-127 follow-ups).
 */
export type Availability = "live" | "coming_soon";

export interface ConnectorDef {
  /** Stable id — matches the backend connector's `name`. */
  id: string;
  name: string;
  category: ConnectorCategory;
  /** Short label for what this source feeds (e.g. "Vector DB cost", "Revenue events"). */
  feeds: string;
  description: string;
  liveKey: LiveKey;
  availability: Availability;
}

export type ConnectorState = "connected" | "available" | "coming_soon";

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
    availability: "live",
  },
  {
    id: "pinecone",
    name: "Pinecone",
    category: "cost",
    feeds: "Vector DB cost",
    description: "Vector database read/write/storage usage attributed to features.",
    liveKey: { kind: "cost-layer", layer: "vector" },
    availability: "coming_soon",
  },
  {
    id: "tavily",
    name: "Tavily",
    category: "cost",
    feeds: "Tool-call cost",
    description: "Search / tool API usage billed per call.",
    liveKey: { kind: "cost-layer", layer: "tools" },
    availability: "coming_soon",
  },
  {
    id: "aws_cost_explorer",
    name: "AWS Cost Explorer",
    category: "cost",
    feeds: "Compute cost",
    description: "Cloud billing line items for compute backing the AI workload.",
    liveKey: { kind: "cost-layer", layer: "compute" },
    availability: "coming_soon",
  },
  {
    id: "vercel",
    name: "Vercel",
    category: "cost",
    feeds: "Egress cost",
    description: "Edge/serverless and bandwidth usage for the serving tier.",
    liveKey: { kind: "cost-layer", layer: "egress" },
    availability: "coming_soon",
  },
  {
    id: "segment",
    name: "Segment",
    category: "revenue",
    feeds: "Revenue events",
    description: "CDP track events — the value side of ROI attribution.",
    liveKey: { kind: "revenue-source", source: "segment" },
    availability: "coming_soon",
  },
  {
    id: "rudderstack",
    name: "RudderStack",
    category: "revenue",
    feeds: "Revenue events",
    description: "Open-source CDP track events (Segment-compatible payloads).",
    liveKey: { kind: "revenue-source", source: "rudderstack" },
    availability: "coming_soon",
  },
  {
    id: "stripe",
    name: "Stripe",
    category: "revenue",
    feeds: "Monetary events",
    description: "Payments, subscriptions and refunds as monetary business events.",
    liveKey: { kind: "revenue-source", source: "stripe" },
    availability: "live",
  },
  {
    id: "hubspot",
    name: "HubSpot",
    category: "revenue",
    feeds: "Lifecycle events",
    description: "Deal-stage / lifecycle changes (e.g. closed-won) for attribution.",
    liveKey: { kind: "revenue-source", source: "hubspot" },
    availability: "coming_soon",
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
    let state: ConnectorState;
    if (records > 0) {
      state = "connected";
    } else if (def.availability === "coming_soon") {
      state = "coming_soon";
    } else {
      state = "available";
    }
    return { ...def, records, lastAt, state };
  });
}

export function connectedCount(rows: ConnectorStatus[]): number {
  return rows.filter((r) => r.state === "connected").length;
}

/** Number of rows that aren't connected today but are claimed to be possible to connect now. */
export function liveAvailableCount(rows: ConnectorStatus[]): number {
  return rows.filter((r) => r.availability === "live").length;
}

export function comingSoonCount(rows: ConnectorStatus[]): number {
  return rows.filter((r) => r.state === "coming_soon").length;
}

/**
 * Typed fallback activity for the cost / revenue source rows. Used when the gateway and
 * ClickHouse are both unreachable so a fresh clone / CI keeps rendering. Real per-tenant
 * third-party integration status now comes from the gateway's
 * `GET /v1/tenant/integrations/status` (CTO-117) — this map is the demo-friendly fallback
 * rather than the primary data path.
 */
export const mockActivity: ConnectorActivity = {
  records: { llm_proxy: 4120, stripe: 86 },
  lastAt: {
    llm_proxy: "2026-05-31T18:40:00Z",
    stripe: "2026-05-31T17:05:00Z",
  },
};

export const mockConnectorStatuses: ConnectorStatus[] = applyActivity(CONNECTORS, mockActivity);
