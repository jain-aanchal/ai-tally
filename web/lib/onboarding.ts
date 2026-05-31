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
  tenantKey: string; // X-Tenant-Key — scoped ingest key, never the customer's OpenAI key
  proxyBaseUrl: string; // OPENAI_BASE_URL the customer points their SDK at
}

/**
 * The copy-paste env block a new tenant pastes to route OpenAI traffic through the proxy (CTO-39).
 * We only ever set the *base URL* and our *tenant key*; the customer's real OpenAI key stays theirs
 * and never appears here.
 */
export function proxyEnvSnippet(creds: TenantProxyCredentials): string {
  return [
    `export OPENAI_BASE_URL="${creds.proxyBaseUrl}"`,
    `export TALLY_TENANT_KEY="${creds.tenantKey}"`,
    `# Your OPENAI_API_KEY is unchanged — it stays in your environment and is never sent to us.`,
  ].join("\n");
}

/** Same config as a Python SDK snippet, for teams who instrument in-process instead of via proxy. */
export function proxyPythonSnippet(creds: TenantProxyCredentials): string {
  return [
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

/** Build the checklist from raw progress timestamps. */
export function deriveChecklist(p: OnboardingProgress): ChecklistStep[] {
  return [
    {
      id: "signed_up",
      title: "Create your account",
      hint: "Done — welcome.",
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
      hint: "We'll detect the first trace automatically — target under 5 minutes.",
      done: p.firstTraceAt !== null,
      targetMs: TIME_TO_FIRST_TRACE_TARGET_MS,
    },
    {
      id: "first_dashboard",
      title: "See your first dashboard",
      hint: "Cost and agent views populate as traces flow in — target within 24 hours.",
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

export function activationStatus(p: OnboardingProgress): ActivationStatus {
  const steps = deriveChecklist(p);
  const ttft = timeToFirstTraceMs(p);
  return {
    activated: p.firstTraceAt !== null,
    withinTarget: ttft !== null && ttft <= TIME_TO_FIRST_TRACE_TARGET_MS,
    timeToFirstTraceMs: ttft,
    completedSteps: steps.filter((s) => s.done).length,
    totalSteps: steps.length,
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
