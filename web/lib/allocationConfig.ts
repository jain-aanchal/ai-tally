// SPDX-License-Identifier: Apache-2.0
// Per-tenant shared-cost allocation rule, read via the gateway (CTO-193, plan C2).
//
// The rule decides how compute and egress are split across accounts on /cost-per-customer, which on
// current data is roughly half of every figure on that page. It used to be a constant compiled into
// the dashboard; it is now a tenant config row (GET/POST /v1/tenant/allocation-config). This module
// is the server-only reader, same rule as lib/accountLabels.ts and lib/unitEconomicsConfig.ts: the
// web app never touches Postgres directly.
//
// WHY this returns a source and not just a rule. The page names the rule beside the column it
// produced, and "pro rata, the default" is a different claim from "pro rata, chosen by this
// tenant". A reader deciding whether to argue with a number needs to know which one they are
// looking at, and a third case exists that is more important than either: the gateway was
// unreachable, so we applied the default WITHOUT being able to check whether this tenant had
// chosen something else. Reporting that as "the default" would be a quiet false claim about the
// tenant's own configuration, so it is its own source and the page says so.

import { DEFAULT_ALLOCATION_RULE, type AllocationRule, ALLOCATION_RULES } from "./allocation";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";
const TENANT = process.env.TALLY_TENANT_ID ?? "local-dev";

/** A slow gateway must not hold a page render open. Same budget lib/accountLabels.ts uses. */
const TIMEOUT_MS = 2000;

/** Wire shape of the gateway's allocation-config response (snake_case, as Postgres spells it). */
export interface AllocationConfigApi {
  allocation_rule?: unknown;
  configured?: unknown;
  default_rule?: unknown;
  available_rules?: unknown;
  config?: { updated_at?: unknown; updated_by?: unknown } | null;
}

/** Where the rule in force came from. Drives what the page is entitled to claim about it. */
export type AllocationRuleSource =
  /** A row exists: this tenant chose the rule. */
  | "tenant"
  /** No row: nobody chose it, so the product default applies. */
  | "default"
  /** The gateway could not be asked. The default applies, but we cannot say the tenant wanted it. */
  | "unavailable";

export interface AllocationRuleSetting {
  /** The rule to apply. Always a usable value, on every path including failure. */
  rule: AllocationRule;
  source: AllocationRuleSource;
  /** ISO timestamp of the tenant's last change, when there is one. */
  updatedAt: string | null;
  updatedBy: string | null;
}

/** The setting a page falls back to when the tenant's own choice cannot be read. */
export function fallbackSetting(source: Exclude<AllocationRuleSource, "tenant">): AllocationRuleSetting {
  return { rule: DEFAULT_ALLOCATION_RULE, source, updatedAt: null, updatedBy: null };
}

/**
 * Map the gateway's response onto the setting the page renders.
 *
 * An unrecognised rule string falls back to the default and reports `unavailable` rather than
 * `tenant`. That combination is deliberate: this dashboard cannot apply a rule it does not
 * implement, so it applies the default, and claiming the tenant chose the default when they chose
 * something else would be worse than admitting the rule could not be determined. It only happens
 * when a gateway is newer than the dashboard, which is exactly when a quiet lie is most likely to
 * go unnoticed.
 */
export function settingFromApi(body: AllocationConfigApi | null): AllocationRuleSetting {
  if (!body) return fallbackSetting("unavailable");
  const raw = body.allocation_rule;
  const known = (ALLOCATION_RULES as readonly string[]).includes(raw as string);
  if (typeof raw !== "string" || !known) return fallbackSetting("unavailable");
  if (body.configured !== true) return fallbackSetting("default");
  const cfg = body.config ?? null;
  return {
    rule: raw as AllocationRule,
    source: "tenant",
    updatedAt: typeof cfg?.updated_at === "string" ? cfg.updated_at : null,
    updatedBy: typeof cfg?.updated_by === "string" ? cfg.updated_by : null,
  };
}

/**
 * The tenant's allocation rule, or the default when they have not chosen one.
 *
 * Never throws and never returns null: a page that cannot read this config still has to render
 * numbers, and refusing to allocate because a config endpoint timed out would be a worse answer
 * than allocating by the default and saying which rule was used. The failure is carried in
 * `source`, not in the absence of a value.
 */
export async function queryAllocationRule(): Promise<AllocationRuleSetting> {
  try {
    const res = await fetch(`${GATEWAY_URL}/v1/tenant/allocation-config`, {
      headers: { "x-tenant-id": TENANT },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) {
      console.warn(
        `[accounts] /v1/tenant/allocation-config HTTP ${res.status}; applying the default rule`,
      );
      return fallbackSetting("unavailable");
    }
    return settingFromApi((await res.json()) as AllocationConfigApi);
  } catch (err) {
    console.warn(
      "[accounts] /v1/tenant/allocation-config unreachable:",
      (err as Error).message,
    );
    return fallbackSetting("unavailable");
  }
}
