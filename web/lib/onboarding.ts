// SPDX-License-Identifier: Apache-2.0
// Guided-onboarding model + helpers (CTO-91). The activation funnel that makes self-serve work:
// signup → copy the proxy config → first trace arrives (<5 min) → first dashboard (<24h).
//
// Pure helpers here (typed like the eventual control-plane shapes); the live first-trace detector
// and funnel-event sink live in the server-only store + route handlers.

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
  /** epoch ms when the stage was reached */
  at: number;
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

export interface OnboardingProgress {
  signedUpAt: number;
  copiedConfigAt: number | null;
  firstTraceAt: number | null;
  firstDashboardAt: number | null;
}

/**
 * Extra evidence the checklist should honour beyond the funnel's own timestamps (#320).
 *
 * `firstTraceProven` is the coverage probe saying a span exists. It ticks the step but deliberately
 * does NOT set `firstTraceAt`: the probe proves that a trace arrived, not when, and back-filling the
 * clock would turn "we found spans just now" into a time-to-first-trace measurement nobody took.
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
      done: p.firstTraceAt !== null || evidence.firstTraceProven === true,
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

/** ms from signup to first trace, or null if no trace yet. */
export function timeToFirstTraceMs(p: OnboardingProgress): number | null {
  if (p.firstTraceAt === null) return null;
  return Math.max(0, p.firstTraceAt - p.signedUpAt);
}

export interface ActivationStatus {
  activated: boolean; // first trace received
  withinTarget: boolean; // ...and within the 5-min target
  timeToFirstTraceMs: number | null;
  completedSteps: number;
  totalSteps: number;
}

export function activationStatus(
  p: OnboardingProgress,
  evidence: ProgressEvidence = {},
): ActivationStatus {
  const steps = deriveChecklist(p, evidence);
  // Stays null when only the probe proved the trace: the arrival was never timed, so there is no
  // duration to report and the readout renders nothing rather than a figure off the wall clock.
  const ttft = timeToFirstTraceMs(p);
  return {
    activated: p.firstTraceAt !== null || evidence.firstTraceProven === true,
    withinTarget: ttft !== null && ttft <= TIME_TO_FIRST_TRACE_TARGET_MS,
    timeToFirstTraceMs: ttft,
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
  /** Spans proving a trace arrived, or null when we could not count them. Never 0 standing in. */
  provingSpans: number | null;
  /** Why the evidence says what it says. Always present, so a blank is never unexplained. */
  reason: string;
}

/** "1 span" / "3 spans". The panel and step 2 both count spans, so both use this (#320, item 4). */
export function spanCountLabel(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? "span" : "spans"}`;
}

/**
 * Derive step 2's first-trace evidence from the same per-layer coverage the panel renders.
 *
 * `covered` is only reachable through a positive span count (see parseCoverage), so a "received"
 * here is always backed by a span the panel is showing in the same breath. When no layer is covered
 * but the probe did read at least one layer, the honest answer is "waiting". When every layer came
 * back unknown, the answer is "we could not tell", carrying the probe's own reason.
 */
export function traceEvidenceFromCoverage(
  layers: readonly { state: string; provingSpans: number | null; reason: string }[],
): TraceEvidence {
  const covered = layers.filter((l) => l.state === "covered" && (l.provingSpans ?? 0) > 0);
  if (covered.length > 0) {
    const spans = covered.reduce((sum, l) => sum + (l.provingSpans ?? 0), 0);
    const layerWord = covered.length === 1 ? "layer" : "layers";
    return {
      state: "received",
      provingSpans: spans,
      reason: `${spanCountLabel(spans)} across ${covered.length} ${layerWord} prove traces are arriving`,
    };
  }
  const readable = layers.filter((l) => l.state !== "unknown");
  if (readable.length === 0) {
    return {
      state: "unknown",
      provingSpans: null,
      reason:
        layers[0]?.reason ??
        "the coverage probe has not answered yet, so we cannot tell whether a trace has arrived",
    };
  }
  return {
    state: "waiting",
    provingSpans: 0,
    reason: "the coverage probe ran and found no span for any layer yet",
  };
}

/** Format a short ms duration as "3.4s" / "2m 10s" for the activation readout. */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.round(ms / 100) / 10;
  if (s < 60) return `${s}s`;
  const m = Math.floor(ms / 60000);
  const rem = Math.round((ms - m * 60000) / 1000);
  return `${m}m ${rem}s`;
}
