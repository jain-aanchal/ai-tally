// SPDX-License-Identifier: Apache-2.0
// Guided-onboarding model + helpers (CTO-91). The activation funnel that makes self-serve work:
// signup → copy the proxy config → first trace arrives (<5 min) → first dashboard (<24h).
//
// Pure helpers here (typed like the eventual control-plane shapes); the funnel-event sink lives in
// the server-only store + route handlers. First-trace evidence comes from the coverage probe, not
// from a stored timestamp (#329, and see OnboardingProgress).

// The activation targets from spec §15. These are the success metrics the checklist is tied to.
export const TIME_TO_FIRST_TRACE_TARGET_MS = 5 * 60 * 1000; // 5 minutes
export const TIME_TO_FIRST_DASHBOARD_TARGET_MS = 24 * 60 * 60 * 1000; // 24 hours

// Ordered funnel stages. Order is load-bearing: each implies the previous is done.
export const FUNNEL_STAGES = [
  "signed_up",
  "viewed_setup",
  "copied_config",
  "first_trace",
  "first_dashboard",
] as const;
export type FunnelStage = (typeof FUNNEL_STAGES)[number];

export interface FunnelEvent {
  stage: FunnelStage;
  /** epoch ms when the stage was reached, or when it was NOTICED if `noticed` is set */
  at: number;
  /**
   * True when `at` is when we noticed the stage rather than when it happened (#329).
   *
   * The coverage probe is the only thing that can tell us a trace arrived, and it proves arrival
   * without proving arrival TIME. The stage is still worth recording, so it is recorded with this
   * flag on it: a consumer measuring activation duration must skip these, because subtracting
   * signup from a noticing time measures how long the tab was open, not time to first trace.
   */
  noticed?: boolean;
}

export interface TenantProxyCredentials {
  tenantKey: string; // X-Tenant-Key, a scoped ingest key, never the customer's OpenAI key
  proxyBaseUrl: string; // OPENAI_BASE_URL the customer points their SDK at
  /**
   * True when these are placeholders rather than the tenant's provisioned values (#320).
   *
   * A stand-in key rendered in the same monospace block a real one would occupy reads as real, so
   * the fact that it is a placeholder has to travel with the value itself. Every surface that shows
   * the credentials then says so: the on-page banner AND the text the developer copies.
   */
  isExample?: boolean;
}

/** Placeholder-credential wording. One string, so the banner and the copied snippet agree (#320). */
export const EXAMPLE_CREDS_NOTICE =
  "Example values, not this tenant's provisioned credentials. Replace them with the key and proxy URL from your workspace settings before running this.";

/**
 * The copy-paste env block a new tenant pastes to route OpenAI traffic through the proxy (CTO-39).
 * We only ever set the *base URL* and our *tenant key*; the customer's real OpenAI key stays theirs
 * and never appears here.
 */
export function proxyEnvSnippet(creds: TenantProxyCredentials): string {
  return [
    // #320: the placeholder warning rides inside the copied text too. A developer who has pasted
    // this into a terminal has left the page behind, so an on-page-only badge never reaches them.
    ...(creds.isExample ? [`# ${EXAMPLE_CREDS_NOTICE}`] : []),
    `export OPENAI_BASE_URL="${creds.proxyBaseUrl}"`,
    `export TALLY_TENANT_KEY="${creds.tenantKey}"`,
    `# Your OPENAI_API_KEY is unchanged. It stays in your environment and is never sent to us.`,
  ].join("\n");
}

/** Same config as a Python SDK snippet, for teams who instrument in-process instead of via proxy. */
export function proxyPythonSnippet(creds: TenantProxyCredentials): string {
  return [
    ...(creds.isExample ? [`# ${EXAMPLE_CREDS_NOTICE}`, ``] : []),
    `from openai import OpenAI`,
    ``,
    `client = OpenAI(`,
    `    base_url="${creds.proxyBaseUrl}",`,
    `    default_headers={"X-Tenant-Key": "${creds.tenantKey}"},`,
    `)`,
  ].join("\n");
}

export type ChecklistStepId =
  | "signed_up"
  | "copied_config"
  | "first_trace"
  | "first_dashboard";

export interface ChecklistStep {
  id: ChecklistStepId;
  title: string;
  hint: string;
  done: boolean;
  /** an activation deadline for this step, if it has one */
  targetMs?: number;
}

/**
 * Funnel timestamps we can actually take.
 *
 * There is deliberately no `firstTraceAt` (#329). Nothing in the product measures when a tenant's
 * first trace arrived: the coverage probe reports counts, not arrival times, and the demo button
 * that used to stamp the clock is gone. The field it replaced was written by nothing, which left
 * time-to-first-trace permanently null and the "under the 5-minute target" readout unable to fire.
 * A timestamp with no source is not an unknown value we render as a blank, it is a measurement we
 * do not take, so the model does not carry a slot for it. Restoring it means restoring a real
 * arrival signal from ingest at the same time.
 */
export interface OnboardingProgress {
  signedUpAt: number;
  copiedConfigAt: number | null;
  firstDashboardAt: number | null;
}

/**
 * Extra evidence the checklist should honour beyond the funnel's own timestamps (#320).
 *
 * `firstTraceProven` is the coverage probe saying a trace arrived. It is the ONLY evidence for that
 * step now (#329): the probe proves arrival, not arrival time, so nothing here is stamped with a
 * clock reading that would turn "we found spans just now" into a duration nobody measured.
 */
export interface ProgressEvidence {
  firstTraceProven?: boolean;
}

/** Build the checklist from raw progress timestamps, plus any probe evidence (#320). */
export function deriveChecklist(
  p: OnboardingProgress,
  evidence: ProgressEvidence = {},
): ChecklistStep[] {
  return [
    {
      id: "signed_up",
      title: "Create your account",
      hint: "Done. Welcome.",
      done: true,
    },
    {
      id: "copied_config",
      title: "Point your app at the proxy",
      hint: "Copy the config below into your environment.",
      done: p.copiedConfigAt !== null,
    },
    {
      id: "first_trace",
      title: "Send your first request",
      hint: "We'll detect the first trace automatically. Target: under 5 minutes.",
      done: evidence.firstTraceProven === true,
      targetMs: TIME_TO_FIRST_TRACE_TARGET_MS,
    },
    {
      id: "first_dashboard",
      title: "See your first dashboard",
      hint: "Cost and agent views populate as traces flow in. Target: within 24 hours.",
      done: p.firstDashboardAt !== null,
      targetMs: TIME_TO_FIRST_DASHBOARD_TARGET_MS,
    },
  ];
}

/**
 * Activation state.
 *
 * #329: `timeToFirstTraceMs` and `withinTarget` used to live here and could never be anything but
 * null/false in the running app, because nothing measured the arrival. They are gone rather than
 * left as fields no code path can populate; the 5-minute target survives as the checklist hint,
 * which states a goal instead of reporting a result.
 */
export interface ActivationStatus {
  activated: boolean; // a trace has been proven to arrive
  completedSteps: number;
  totalSteps: number;
}

export function activationStatus(
  p: OnboardingProgress,
  evidence: ProgressEvidence = {},
): ActivationStatus {
  const steps = deriveChecklist(p, evidence);
  return {
    activated: evidence.firstTraceProven === true,
    completedSteps: steps.filter((s) => s.done).length,
    totalSteps: steps.length,
  };
}

// -------------------------------------------------------------------------------------------
// First-trace evidence (#320).
//
// Step 2 of the onboarding page used to read `progress.firstTraceAt` out of the client-side
// onboarding store, while the coverage panel directly under it reported layers a real span had
// already proven. Two sources for one fact, so the page argued with itself: "Waiting for your first
// trace…" sitting on top of "LLM calls · Flowing · 12 spans".
//
// There is only one piece of evidence that a trace arrived, and it is the coverage probe's span
// counts, so step 2 reads those instead. Deriving it here rather than in the component keeps it
// pure and unit-tested, and keeps the third state honest: a probe that could not be read means we
// do not know whether a trace arrived, which is not the same claim as "none has".
// -------------------------------------------------------------------------------------------

/** "received" needs a proving span; "unknown" means the probe could not be read (never "waiting"). */
export type TraceEvidenceState = "received" | "waiting" | "unknown";

export interface TraceEvidence {
  state: TraceEvidenceState;
  /**
   * Spans proving a trace arrived, or null when there is no span count to report: either we could
   * not count them, or the only covered layer counts rollup rows rather than spans (#329). Never 0
   * standing in for either.
   */
  provingSpans: number | null;
  /** Why the evidence says what it says. Always present, so a blank is never unexplained. */
  reason: string;
}

/** "1 span" / "3 spans". The panel and step 2 both count spans, so both use this (#320, item 4). */
export function spanCountLabel(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? "span" : "spans"}`;
}

/**
 * Label a layer's proving count in the unit that layer actually counts (#329).
 *
 * Four of the five layers count spans; the account layer counts rows of daily_account_rollup, which
 * its own probe reason already says. The panel used to print all five through spanCountLabel, so a
 * row reading "5,914 rollup row(s) carry a non-empty AccountIdHash" was captioned "5,914 spans".
 */
export function provingCountLabel(layer: string | undefined, n: number): string {
  if (layer === ROLLUP_COUNTED_LAYER) {
    return `${n.toLocaleString()} ${n === 1 ? "rollup row" : "rollup rows"}`;
  }
  return spanCountLabel(n);
}

/**
 * The one layer whose proving count is NOT spans (#329).
 *
 * The gateway probe counts the four operation layers out of otel_spans, one row per span, keyed by
 * GenAiOperation, so those four are disjoint and summable. The account layer is counted out of
 * daily_account_rollup instead (one row per account/day/feature/operation), and its own reason
 * string says "rollup row(s)". Adding the two together produced a figure that was neither: it moved
 * by ~2,000 when PR #325 merely rebuilt the rollup, which a span count cannot do.
 */
const ROLLUP_COUNTED_LAYER = "account";

const PROBE_SILENT_REASON =
  "the coverage probe has not answered yet, so we cannot tell whether a trace has arrived";

/**
 * Derive step 2's first-trace evidence from the same per-layer coverage the panel renders.
 *
 * `covered` is only reachable through a positive proving count (see parseCoverage), so a "received"
 * here is always backed by evidence the panel is showing in the same breath.
 *
 * Two rules keep the rendered sentence true:
 *
 *  - Only the four operation layers contribute to the span TOTAL, because only they count spans
 *    (see ROLLUP_COUNTED_LAYER). The account layer still proves traces arrived, since a rollup row
 *    cannot exist without spans behind it, so it can carry "received" on its own; it just carries
 *    it with a null count rather than a number in the wrong unit.
 *  - "waiting" is a definite negative, so it is only claimed when every layer was readable and none
 *    of them found anything. A mixed report (some layers readable, some unknown) means the layers
 *    that could have proven a trace were not all read, and the honest answer is that we cannot tell.
 */
export function traceEvidenceFromCoverage(
  layers: readonly { layer?: string; state: string; provingSpans: number | null; reason: string }[],
): TraceEvidence {
  const proven = layers.filter((l) => l.state === "covered" && (l.provingSpans ?? 0) > 0);
  const spanLayers = proven.filter((l) => l.layer !== ROLLUP_COUNTED_LAYER);
  if (spanLayers.length > 0) {
    const spans = spanLayers.reduce((sum, l) => sum + (l.provingSpans ?? 0), 0);
    const layerWord = spanLayers.length === 1 ? "layer" : "layers";
    return {
      state: "received",
      provingSpans: spans,
      reason: `${spanCountLabel(spans)} across ${spanLayers.length} ${layerWord} prove traces are arriving`,
    };
  }
  if (proven.length > 0) {
    // Only the account layer is covered: attributed rollup rows exist, which they cannot without
    // spans behind them. So a trace did arrive, and we say so, without a span count we do not have.
    return {
      state: "received",
      provingSpans: null,
      reason:
        "per-customer attribution has rows, which only exist once traces arrive, but no layer " +
        "returned a span count so there is no number of spans to report",
    };
  }
  const unreadable = layers.filter((l) => l.state === "unknown");
  if (unreadable.length === layers.length) {
    return {
      state: "unknown",
      provingSpans: null,
      reason: layers[0]?.reason ?? PROBE_SILENT_REASON,
    };
  }
  if (unreadable.length > 0) {
    const layerWord = unreadable.length === 1 ? "layer" : "layers";
    return {
      state: "unknown",
      provingSpans: null,
      reason:
        `no span was found on the layers we could read, but ${unreadable.length} ${layerWord} ` +
        `could not be read (${unreadable[0].reason}), so we cannot tell whether a trace has arrived`,
    };
  }
  return {
    state: "waiting",
    provingSpans: 0,
    reason: "the coverage probe read every layer and found no span for any of them yet",
  };
}
