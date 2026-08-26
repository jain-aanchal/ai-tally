// SPDX-License-Identifier: Apache-2.0
// Mock data for the Data Quality surface (CTO-79). Typed for the eventual API.

export type Health = "good" | "warn" | "bad";

export interface AttributionByFeature {
  feature: string;
  rate: number; // 0..1
  events7d: number;
}

export interface ContextDropsByService {
  service: string;
  sdkVersion: string;
  drops24h: number;
  /** CTO-118: total span count over the same 24h window. 0 → service inactive → render "—". */
  spans24h?: number;
}

export interface CalibrationDay {
  date: string;
  estimatedMicroUsd: number;
  reconciledMicroUsd: number;
}

export interface SampleByStratum {
  stratum: "body" | "mid" | "tail";
  rate: number; // 0..1, average configured keep rate seen in this stratum over the window
  /**
   * 0..1 fractional half-width of the 95% CI on extrapolated cost. `null` when fewer than 30
   * kept spans landed in this stratum over the window — Wilson-flavoured estimators get
   * uselessly wide below that. Render `—`, never a fabricated tight band (CTO-119).
   */
  ciHalfWidthPct: number | null;
  /** kept-span count in this stratum over the window (used to gate the CI). */
  spans: number;
}

/**
 * One user observed against more than one account (CTO-184).
 *
 * One user belongs to one account; multi-account users are explicitly not supported. When a user
 * is stitched to two accounts the pipeline attributes NOTHING for them: it does not split the
 * cost, does not duplicate it, and does not pick first-seen. Duplicating would inflate the tenant
 * total so per-account spend would stop summing to what /cost reports, which is the one thing
 * that would make the whole per-customer surface untrustworthy.
 *
 * Withholding is the safe behaviour but it is not a silent one, which is what this type is for:
 * the fix lives in the tenant's CRM and nobody can make it if nobody can see the conflict.
 */
export interface AccountStitchConflict {
  /** The ambiguous user's hash. Truncated for display; never a raw id. */
  userIdHash: string;
  /** Every distinct account hash observed for this user, sorted. Always length >= 2. */
  accounts: string[];
  /** Spend held back over the last 30d because of this ambiguity. */
  withheldMicroUsd: number;
  /** Spans behind that figure. */
  spans30d: number;
}

/**
 * Account-dimension coverage and its confidence (CTO-184).
 *
 * `directAccounts` and `stitchedAccounts` are the two ways an account can become known, and a
 * consumer tells them apart by which one a hash appears in: a directly-tagged account was stamped
 * on the span itself at emit time (`otel_spans.AccountIdHash != ''`, CTO-180) and is as certain as
 * the span; a stitched account was inferred from an `account_id` edge asserted by a CRM or CDP
 * connector in `identity_graph` and is only as good as that connector's data. The UI renders the
 * second with lower confidence.
 */
export interface AccountStitching {
  /** Accounts stamped directly on spans over the window. */
  directAccounts: number;
  /** Accounts known only from identity-graph `account_id` edges. */
  stitchedAccounts: number;
  /** Users carrying at least one `account_id` edge. */
  stitchedUsers: number;
  /** Users the stitcher refuses to attribute, because they resolve to more than one account. */
  conflicts: AccountStitchConflict[];
}

export interface DataQualityReport {
  overall: {
    attributionRate: number;
    contextDropCount24h: number;
    estimateCalibration: number; // |est - recon| / recon, last reconciled period
    effectiveSampleRate: number; // weighted across strata
  };
  attribution: AttributionByFeature[];
  contextDrops: ContextDropsByService[];
  calibration: CalibrationDay[];
  sampling: SampleByStratum[];
  /**
   * CTO-184. Optional so a cached or older payload still type-checks; the page treats an absent
   * value the same as "no account dimension instrumented yet", which is the honest reading.
   */
  accountStitching?: AccountStitching;
}

/**
 * Total spend withheld from per-account attribution because of multi-account users (CTO-184).
 * Pure so the page and the tests agree on one definition of "how much this is costing us".
 */
export function withheldByConflicts(conflicts: AccountStitchConflict[]): number {
  return conflicts.reduce((sum, c) => sum + c.withheldMicroUsd, 0);
}

/**
 * Health of the account-stitching signal. Any conflict at all is a `bad`: it is not a threshold
 * question, it is a tenant CRM asserting two contradictory facts about the same person, and the
 * consequence is that we report nothing for them. Zero conflicts with stitching in use is `good`;
 * zero conflicts with nothing stitched is also `good`, because nothing is wrong, there is just
 * nothing there.
 */
export function classifyAccountStitching(s: AccountStitching | undefined): Health {
  if (!s || s.conflicts.length === 0) return "good";
  return "bad";
}

export function classify(metric: "attribution" | "drops" | "calibration", v: number): Health {
  if (metric === "attribution") return v >= 0.9 ? "good" : v >= 0.75 ? "warn" : "bad";
  if (metric === "drops") return v === 0 ? "good" : v < 10 ? "warn" : "bad";
  // calibration: smaller is better
  return v < 0.03 ? "good" : v < 0.07 ? "warn" : "bad";
}

export const dq: DataQualityReport = {
  overall: {
    attributionRate: 0.84,
    contextDropCount24h: 3,
    estimateCalibration: 0.018,
    effectiveSampleRate: 0.22,
  },
  attribution: [
    { feature: "research_agent", rate: 0.91, events7d: 1820 },
    { feature: "support_triage", rate: 0.79, events7d: 4_320 },
    { feature: "inline_writer", rate: 0.88, events7d: 1180 },
    { feature: "chatbot", rate: 0.73, events7d: 980 },
    { feature: "smart_search", rate: 0.82, events7d: 612 },
    { feature: "summarize", rate: 0.0, events7d: 0 },
  ],
  contextDrops: [
    { service: "api-prod", sdkVersion: "py-0.0.1", drops24h: 2, spans24h: 184_000 },
    { service: "worker-prod", sdkVersion: "py-0.0.1", drops24h: 1, spans24h: 96_400 },
    { service: "edge-proxy", sdkVersion: "go-0.0.1", drops24h: 0, spans24h: 312_900 },
  ],
  calibration: [
    { date: "2026-06-06", estimatedMicroUsd: 1_420_000_000, reconciledMicroUsd: 1_402_000_000 },
    { date: "2026-06-07", estimatedMicroUsd: 1_510_000_000, reconciledMicroUsd: 1_488_000_000 },
    { date: "2026-06-08", estimatedMicroUsd: 1_580_000_000, reconciledMicroUsd: 1_561_000_000 },
    { date: "2026-06-09", estimatedMicroUsd: 1_610_000_000, reconciledMicroUsd: 1_628_000_000 },
    { date: "2026-06-10", estimatedMicroUsd: 1_660_000_000, reconciledMicroUsd: 1_641_000_000 },
    { date: "2026-06-11", estimatedMicroUsd: 1_720_000_000, reconciledMicroUsd: 1_704_000_000 },
    { date: "2026-06-12", estimatedMicroUsd: 1_750_000_000, reconciledMicroUsd: 1_734_000_000 },
  ],
  sampling: [
    { stratum: "tail", rate: 1.0, ciHalfWidthPct: 0.0, spans: 420 },
    { stratum: "mid", rate: 0.5, ciHalfWidthPct: 0.04, spans: 1840 },
    { stratum: "body", rate: 0.1, ciHalfWidthPct: 0.18, spans: 12_600 },
  ],
  // CTO-184. The mock deliberately carries one conflict, because the empty case renders a
  // reassuring green row and the interesting case is the one a reviewer needs to be able to see.
  accountStitching: {
    directAccounts: 18,
    stitchedAccounts: 42,
    stitchedUsers: 1_340,
    conflicts: [
      {
        userIdHash: "9f2c41ab7d0e5583",
        accounts: ["3b7e0c19aa41d2f8", "c04d19e6b7a35510"],
        withheldMicroUsd: 4_820_000,
        spans30d: 312,
      },
    ],
  },
};
