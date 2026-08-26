// SPDX-License-Identifier: Apache-2.0
// Which business events count as revenue, per tenant (CTO-194).
//
// The attribution revenue sum used to be gated on a hardcoded `business_events.Source = 'stripe'`.
// `Source` is an unconstrained LowCardinality(String) stamped by whichever connector ingested the
// row, so every tenant not billing through Stripe had its revenue silently dropped and the
// VALUE/USER and MARGIN/USER columns rendered blank. On the local demo tenant that discarded ~14.8k
// monetary events worth ~$1.7M, all carrying Source='vercel-chatbot-backfill'.
//
// `business_events.ValueType` is the correct discriminator: it is a real ClickHouse enum
// ('monetary'=1,'count'=2,'mrr'=3,'refund'=4). Engagement signals are 'count' and carry no amount;
// money is 'monetary'/'mrr'; refunds must NET OFF rather than be ignored.
//
// Per-tenant config (GET/POST /v1/tenant/revenue-sources/config, backed by Postgres — the web app
// never touches Postgres directly, same rule as web/lib/unitEconomicsConfig.ts) only ever NARROWS
// that default, by naming which sources a tenant considers revenue-bearing. A tenant with no row,
// or an unreachable gateway (CI / fresh clone), gets the defaults, so nothing is broken by absence.

/** Value types that carry money. Mirrors the ClickHouse enum in db/clickhouse/attribution.sql. */
export const POSITIVE_VALUE_TYPES = ["monetary", "mrr"] as const;
export const REFUND_VALUE_TYPE = "refund";

/** Resolved policy the ClickHouse query builds its revenue expression from. */
export interface RevenuePolicy {
  /**
   * Source values that count as revenue, lowercased. `null` means "every source counts" — the
   * default, and deliberately distinct from an empty list (which the gateway rejects, because
   * "nothing is revenue" is a misconfiguration that silently blanks the dashboard).
   */
  sources: string[] | null;
  /**
   * Whether recurring `mrr` amounts are summed alongside one-off `monetary` charges. Default true.
   * Tenants whose biller emits both for the same subscription can turn this off to stop double
   * counting.
   */
  includeMrr: boolean;
}

/** Defaults applied when the tenant has no config row. */
export const DEFAULT_REVENUE_POLICY: RevenuePolicy = { sources: null, includeMrr: true };

/** Wire shape of the gateway's config object (snake_case, matching the Postgres columns). */
export interface RevenueSourceConfigApi {
  revenue_sources: string[] | null;
  include_mrr: boolean;
  created_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

/** Map the gateway's snake_case config onto a resolved policy, falling back to the defaults. */
export function policyFromApi(cfg: RevenueSourceConfigApi | null): RevenuePolicy {
  if (!cfg) return DEFAULT_REVENUE_POLICY;
  const raw = Array.isArray(cfg.revenue_sources) ? cfg.revenue_sources : null;
  const sources = raw
    ? Array.from(new Set(raw.map((s) => String(s).trim().toLowerCase()).filter(Boolean)))
    : null;
  return {
    // An empty array after cleaning means the row said nothing usable. Treat that as "all sources"
    // rather than "no revenue exists" — blanking the dashboard on bad config is the original bug.
    sources: sources && sources.length > 0 ? sources : null,
    includeMrr: cfg.include_mrr !== false,
  };
}

/** The ValueType values that count positively under this policy. */
export function positiveValueTypes(policy: RevenuePolicy): string[] {
  return policy.includeMrr ? [...POSITIVE_VALUE_TYPES] : ["monetary"];
}

/**
 * SQL fragment restricting a `business_events` alias to the tenant's revenue sources, plus the
 * bound parameter it needs. Empty fragment when every source counts.
 *
 * `lower(Source)` matches the normalization the gateway applies on write and mirrors how
 * queryConnectorActivity already compares Source values.
 */
export function revenueSourceFilter(
  policy: RevenuePolicy,
  alias: string,
): { sql: string; params: Record<string, unknown> } {
  if (!policy.sources) return { sql: "", params: {} };
  return {
    sql: `AND lower(${alias}.Source) IN {revenueSources:Array(String)}`,
    params: { revenueSources: policy.sources },
  };
}

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";
const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";

/**
 * Fetch the tenant's revenue policy. Never throws and never returns null: a tenant with no row or
 * an unreachable gateway yields DEFAULT_REVENUE_POLICY, which is the pre-existing behaviour for
 * every tenant that was never configured.
 */
export async function queryRevenuePolicy(): Promise<RevenuePolicy> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/revenue-sources/config`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return DEFAULT_REVENUE_POLICY;
    const body = (await res.json()) as { config: RevenueSourceConfigApi | null };
    return policyFromApi(body.config ?? null);
  } catch {
    return DEFAULT_REVENUE_POLICY;
  }
}
