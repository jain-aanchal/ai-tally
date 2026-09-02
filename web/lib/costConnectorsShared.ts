// SPDX-License-Identifier: Apache-2.0
// Client-safe cost-connector shapes (CTO-176; boundary split CTO-259).
//
// The transport half (`costConnectors.ts`) resolves the tenant via the server-only `getTenant`, so a
// Client Component cannot import from it without dragging the server graph in. These constants,
// types and pure helpers carry no such dependency, so the client table imports them from here while
// `costConnectors.ts` re-exports them for server callers.

/** Connector ids the config endpoints understand. Matches config_admin.ALL_CONNECTORS. */
export const CONFIGURABLE = [
  "aws_cost_explorer",
  "gcp_billing",
  "vercel",
  "cloudflare",
  "aws_egress",
  "vercel_egress",
] as const;
export type ConfigurableConnector = (typeof CONFIGURABLE)[number];

export function isConfigurable(id: string): id is ConfigurableConnector {
  return (CONFIGURABLE as readonly string[]).includes(id);
}

export interface CostConnectorConfig {
  connector: string;
  configured: boolean;
  credentialsRef: string | null;
  /** Whether the stored ref matches a known secret-manager shape. Advisory, drives a hint only. */
  isReference: boolean;
  details: Record<string, unknown>;
  lastRunAt: string | null;
  lastStatus: string | null;
  connectedAt: string | null;
}
