// SPDX-License-Identifier: Apache-2.0
// Server-only in-memory onboarding store backing the live first-trace detector and funnel sink.
//
// In production these are control-plane rows + an ingest signal. In the prototype we keep a single
// in-process record so the "waiting for first trace → received" transition is real and demonstrable
// (the "Send a test trace" button stands in for the customer's first proxied request). State resets
// on server restart — fine for a mock; `npm run dev/build/test` never need infra.

import {
  type FunnelEvent,
  type FunnelStage,
  type OnboardingProgress,
  type TenantProxyCredentials,
} from "./onboarding";

interface StoreState {
  progress: OnboardingProgress;
  funnel: FunnelEvent[];
  creds: TenantProxyCredentials;
}

function freshState(): StoreState {
  return {
    progress: {
      signedUpAt: Date.now(),
      copiedConfigAt: null,
      firstTraceAt: null,
      firstDashboardAt: null,
    },
    funnel: [{ stage: "signed_up", at: Date.now() }],
    creds: {
      tenantKey: "tk_demo_3f9c2a7b",
      proxyBaseUrl: "https://proxy.ai-tally.dev/v1",
    },
  };
}

// Survive Next.js dev hot-reload by stashing on globalThis.
const g = globalThis as unknown as { __tallyOnboarding?: StoreState };
function state(): StoreState {
  if (!g.__tallyOnboarding) g.__tallyOnboarding = freshState();
  return g.__tallyOnboarding;
}

export function getProgress(): OnboardingProgress {
  return { ...state().progress };
}

export function getCreds(): TenantProxyCredentials {
  return { ...state().creds };
}

export function getFunnel(): FunnelEvent[] {
  return [...state().funnel];
}

export function recordFunnel(stage: FunnelStage): FunnelEvent {
  const ev: FunnelEvent = { stage, at: Date.now() };
  const s = state();
  s.funnel.push(ev);
  // Mirror the stage onto progress timestamps (first occurrence wins).
  if (stage === "copied_config" && s.progress.copiedConfigAt === null) {
    s.progress.copiedConfigAt = ev.at;
  }
  if (stage === "first_trace" && s.progress.firstTraceAt === null) {
    s.progress.firstTraceAt = ev.at;
  }
  if (stage === "first_dashboard" && s.progress.firstDashboardAt === null) {
    s.progress.firstDashboardAt = ev.at;
  }
  return ev;
}

/** The "Send a test trace" action — stands in for the customer's first proxied request. */
export function markFirstTrace(): OnboardingProgress {
  if (state().progress.firstTraceAt === null) recordFunnel("first_trace");
  return getProgress();
}

/** Test-only: reset the in-memory store. */
export function __resetOnboarding(): void {
  g.__tallyOnboarding = freshState();
}
