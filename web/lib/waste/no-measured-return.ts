// SPDX-License-Identifier: Apache-2.0
// "Spend with no measured return" waste detector (CTO-232, W5; epic CTO-227).
//
// This is the detector most likely to cry wolf, so its honesty posture is load-bearing. It flags a
// feature that burned real spend over the window yet has NO measured value tracing back to it -- but
// only when the tenant HAS attribution wired somewhere else. The distinction is the whole point:
//
//   * "this whole tenant has no revenue source wired" (no business_events, or none attributed) is an
//     instrumentation gap, not waste. Flagging every costly feature in that state would be a wall of
//     false positives. So when the tenant-wide attribution rate is 0 / null, we emit NOTHING.
//   * "this tenant attributes value elsewhere, but this one costly feature attributes none" is the
//     real signal. Only THEN do we flag the feature, and even then as a flag to INVESTIGATE, never a
//     verdict of pure waste: top-of-funnel work and revenue that is simply not yet wired to the
//     feature look identical to genuine waste from telemetry alone. The reason carries that caveat.
//
// Attribution is NOT re-derived here (CTO-124 owns it): `collectNoMeasuredReturn` reuses
// `queryFeatureEconomics` for the per-feature attribution rate and only queries the windowed spend
// and the tenant-wide attribution rate it needs on top. The pure `detectNoMeasuredReturn` does the
// judgement so it is deterministic and testable without a database.

import type { MicroUSD } from "@/lib/types";
import type { WasteConfidence, WasteFinding, WasteScopeKind } from "@/lib/waste";
import { clampWindowDays } from "@/lib/explore";
import type { DimensionFilters } from "@/lib/filters";
import { micro, queryFeatureEconomics, rowsPCached, tryLive } from "@/lib/clickhouse";

// At or below this attribution rate a scope has, for our purposes, NO measured return: in practice
// `queryFeatureEconomics` returns either ~1.0 (conversions attributed) or null (none), but a small
// epsilon keeps "near zero" from hinging on an exact float.
const NEAR_ZERO_ATTRIBUTION = 0.02;

// Above this tenant-wide attribution rate we treat the tenant's attribution as "otherwise healthy",
// so a feature with none stands out and earns `medium` confidence. Below it (but still > 0) the
// tenant has only sparse attribution wired, so the same feature is a weaker signal -- capped at
// `low`. This is a confidence knob only; it never changes the dollar math.
const TENANT_HEALTHY_ATTRIBUTION = 0.5;

/**
 * One scope's economics as this detector needs them. Intentionally NOT `FeatureEconomics`: that type
 * exposes cost PER USER, and this detector reasons about the scope's whole windowed spend (the
 * at-risk dollars). `attributionRate` is carried straight from `queryFeatureEconomics` so attribution
 * is reused, not recomputed; `null` there means "no measured attribution for this scope".
 */
export interface NoReturnEconRow {
  /** Almost always `feature`; the shape allows `agent` so an agent-scoped caller can reuse it. */
  scopeKind: WasteScopeKind;
  scopeValue: string;
  /** The scope's total spend over the window. Integer micro-USD. Always known. */
  windowSpendMicroUsd: MicroUSD;
  /** 0..1, or `null` for "no measured attribution". From `queryFeatureEconomics`, never re-derived. */
  attributionRate: number | null;
  /**
   * Raw attributed-conversion count for the scope over the window, from `queryFeatureEconomics`.
   * CTO-227 review finding (Bug 3): `attributionRate` is `null` in TWO different states — a feature
   * with genuinely zero attribution AND a feature with some conversions but below the trust floor
   * (MIN_CONVERSIONS_FOR_ECONOMICS). Only the count tells them apart, so the detector gates on this,
   * not on the null rate, to avoid false-flagging a sparse-but-converting feature as pure waste.
   */
  conversions: number;
}

/**
 * PURE detector. Flags each scope that spent real money over the window with no measured return,
 * subject to the honesty gate below. Deterministic: no queries, no clock, no randomness.
 *
 * Honesty gate (CTO-232): if the tenant has NO attribution wired at all (`tenantAttributionRate` is
 * `null` or <= 0), return `[]`. A tenant with no revenue source connected cannot have "unreturned"
 * spend distinguished from "un-instrumented" spend, so flagging anything would be a false positive.
 *
 * When the gate passes, a scope is flagged iff it has `windowSpendMicroUsd > 0` AND an attribution
 * rate at or near zero (or `null`). `recoverableMicroUsd` is the at-risk spend (the windowed cost)
 * because that is the exact figure an investigation would be trying to justify -- but the reason is
 * explicit that this is a flag to investigate, not proven waste. Confidence is capped at `low` when
 * the tenant's own attribution is sparse and is `medium` (never `high`, this detector cannot be sure)
 * when the tenant is otherwise well-attributed.
 */
export function detectNoMeasuredReturn(
  econRows: NoReturnEconRow[],
  tenantAttributionRate: number | null,
): WasteFinding[] {
  // Gate: whole-tenant no-attribution state emits nothing, never a wall of false positives.
  if (tenantAttributionRate === null || tenantAttributionRate <= 0) return [];

  const confidence: WasteConfidence =
    tenantAttributionRate >= TENANT_HEALTHY_ATTRIBUTION ? "medium" : "low";

  const findings: WasteFinding[] = [];
  for (const row of econRows) {
    if (row.windowSpendMicroUsd <= 0) continue; // no spend at risk, nothing to flag
    // CTO-227 review finding (Bug 3): flag ONLY when attribution is genuinely zero. A feature with
    // any attributed conversions (conversions > 0) has measured return, even when its `attributionRate`
    // is null because it sits below the MIN_CONVERSIONS_FOR_ECONOMICS trust floor. Reading that
    // null-because-untrusted rate as "zero attribution" false-flagged sparse-but-converting features
    // for their full spend. Treat conversions > 0 as "unknown / real return", never waste.
    if (row.conversions > 0) continue;
    // Defensive: a positive rate should never coincide with zero conversions, but if it does, honor
    // the measured return rather than flag.
    const hasReturn =
      row.attributionRate !== null && row.attributionRate > NEAR_ZERO_ATTRIBUTION;
    if (hasReturn) continue; // real measured return -> not waste

    findings.push({
      category: "no_measured_return",
      scopeKind: row.scopeKind,
      scopeValue: row.scopeValue,
      // The at-risk spend IS the windowed cost: what an investigation would be sizing.
      recoverableMicroUsd: row.windowSpendMicroUsd,
      windowSpendMicroUsd: row.windowSpendMicroUsd,
      confidence,
      title: `${row.scopeValue}: spend with no measured return`,
      reason:
        "Spend with no measured return: this scope cost real money over the window, yet no " +
        "converting business event traces back to it, while the tenant attributes value " +
        "elsewhere. Caveat: top-of-funnel work, or revenue that is simply not yet wired to this " +
        "scope, looks identical from telemetry alone, so treat this as a flag to investigate, not " +
        "a verdict of pure waste.",
      evidence: {
        windowSpend: row.windowSpendMicroUsd,
        attributedValue: 0,
        tenantAttributionRate,
      },
      drillHref: "/attribution",
    });
  }

  return findings;
}

/** Per-feature windowed spend row as it comes back from ClickHouse. */
interface FeatureSpendRow {
  feature: string;
  cost: string;
}

/** The two scalars of the tenant-wide attribution query. */
interface TenantAttributionRow {
  total_events: string;
  attributed_events: string;
}

/**
 * Live collector. Runs the windowed feature economics (reused for attribution) and the tenant-wide
 * attribution rate against ClickHouse, maps them through the pure detector, and returns `[]` when the
 * data is unavailable (ClickHouse unreachable or economics could not be produced) so a down backend
 * never fabricates findings. `filters.feature`, when set, narrows the scan to those features.
 */
export async function collectNoMeasuredReturn(
  windowDays: number,
  filters: DimensionFilters,
): Promise<WasteFinding[]> {
  // Window is ClickHouse-clock derived (CTO-203) and bound-checked (injection-safe int), matching the
  // form `queryFeatureEconomics` uses so the spend and attribution windows line up.
  const w = clampWindowDays(windowDays);

  // Reuse the per-feature attribution rate; do NOT re-derive it (CTO-124 owns that math).
  const econ = await queryFeatureEconomics(w);
  if (econ === null) return [];
  const attrRateByFeature = new Map(econ.map((e) => [e.feature, e.attributionRate]));
  // CTO-227 review finding (Bug 3): carry the RAW conversion count too, so the detector can tell a
  // genuinely-unattributed feature (0) from a sparse-but-converting one (rate null, conversions > 0).
  const conversionsByFeature = new Map(econ.map((e) => [e.feature, e.conversions]));

  const featureFilter = filters.feature ?? [];
  const hasFeatureFilter = featureFilter.length > 0;

  const live = await tryLive(async (db, tenant) => {
    // Per-feature windowed spend. `w` is a clamped int, so interpolating it is injection-safe (same
    // pattern as the sibling queries in lib/clickhouse.ts). `filters.feature` binds as an array param.
    const spendRows = await rowsPCached<FeatureSpendRow>(
      db,
      `SELECT FeatureTag AS feature, sum(EstimatedCost) AS cost
       FROM otel_spans
       WHERE TenantId = {tenant:String}
         AND Timestamp >= now() - INTERVAL ${w} DAY
         AND FeatureTag != ''
         ${hasFeatureFilter ? "AND FeatureTag IN {features:Array(String)}" : ""}
       GROUP BY FeatureTag`,
      { tenant, features: featureFilter },
    );

    // Tenant-wide attribution rate: distinct business events that got an attribution record over the
    // window / all business events over the window. This is the honesty discriminator -- 0 (or no
    // events at all) means the tenant has no revenue source wired, and the detector then emits [].
    const [attrRow] = await rowsPCached<TenantAttributionRow>(
      db,
      `SELECT
         uniqExact(BusinessEventId) AS total_events,
         uniqExactIf(BusinessEventId, BusinessEventId IN (
           SELECT BusinessEventId FROM attribution_records FINAL
           WHERE TenantId = {tenant:String} AND AttributedTraceTs >= now() - INTERVAL ${w} DAY
         )) AS attributed_events
       FROM business_events FINAL
       WHERE TenantId = {tenant:String} AND OccurredAt >= now() - INTERVAL ${w} DAY`,
      { tenant },
    );

    const totalEvents = attrRow ? parseInt(attrRow.total_events, 10) || 0 : 0;
    const attributedEvents = attrRow ? parseInt(attrRow.attributed_events, 10) || 0 : 0;
    // No business events at all -> null (honest blank), NOT 0: distinct from "events, none attributed"
    // for a reader, though both hit the same [] gate in the pure detector.
    const tenantAttributionRate = totalEvents > 0 ? attributedEvents / totalEvents : null;

    return { spendRows, tenantAttributionRate };
  });
  if (live === null) return [];

  const econRows: NoReturnEconRow[] = live.spendRows.map((r) => ({
    scopeKind: "feature" as const,
    scopeValue: r.feature,
    windowSpendMicroUsd: micro(r.cost),
    attributionRate: attrRateByFeature.get(r.feature) ?? null,
    // A feature with spend but no economics row attributes nothing -> 0 (genuinely zero, flaggable).
    conversions: conversionsByFeature.get(r.feature) ?? 0,
  }));

  return detectNoMeasuredReturn(econRows, live.tenantAttributionRate);
}
