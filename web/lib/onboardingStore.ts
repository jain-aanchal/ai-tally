// SPDX-License-Identifier: Apache-2.0
// Server-only in-memory onboarding store backing the activation funnel sink.
//
// In production these are control-plane rows. In the prototype we keep a single in-process record
// so the funnel is real and demonstrable. State resets on server restart, which is fine for a mock;
// `npm run dev/build/test` never need infra.
//
// #329: there is no first-trace detector here any more, and no "Send a test trace" button behind
// it. Whether a trace arrived is answered by the gateway coverage probe, which the onboarding page
// polls directly, so the store no longer keeps a first-trace timestamp it had no way to measure.

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
      firstDashboardAt: null,
    },
    funnel: [{ stage: "signed_up", at: Date.now() }],
    // #320: these are placeholders, not a provisioned key and endpoint, and they used to render in
    // step 1 as though they were the tenant's own. The provisioning path is control-plane work; in
    // the meantime the values carry `isExample` so every surface that shows them says what they are
    // rather than presenting an invented key as fact (CLAUDE.md, honest under uncertainty).
    creds: {
      tenantKey: "tk_example_replace_me",
      proxyBaseUrl: "https://proxy.example.ai-tally.dev/v1",
      isExample: true,
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

/**
 * Record a funnel stage.
 *
 * `noticed` marks a stage we observed after the fact rather than timed (#329). The onboarding page
 * posts first_trace that way, off the coverage probe: the stage is real, the moment is only when we
 * spotted it, so the event carries the flag and is never mirrored onto a progress timestamp that
 * would then be read as a measured duration. A stage is recorded once; later reports are ignored.
 */
export function recordFunnel(stage: FunnelStage, opts: { noticed?: boolean } = {}): FunnelEvent {
  const s = state();
  const ev: FunnelEvent = { stage, at: Date.now(), ...(opts.noticed ? { noticed: true } : {}) };
  s.funnel.push(ev);
  // Mirror the stage onto progress timestamps (first occurrence wins). Only stages the page itself
  // performs are mirrored; a noticed stage has no measured time to mirror.
  if (opts.noticed) return ev;
  if (stage === "copied_config" && s.progress.copiedConfigAt === null) {
    s.progress.copiedConfigAt = ev.at;
  }
  if (stage === "first_dashboard" && s.progress.firstDashboardAt === null) {
    s.progress.firstDashboardAt = ev.at;
  }
  return ev;
}

/** Whether this stage has already been recorded, so a repeated report does not pile up events. */
export function hasFunnelStage(stage: FunnelStage): boolean {
  return state().funnel.some((e) => e.stage === stage);
}

/** Test-only: reset the in-memory store. */
export function __resetOnboarding(): void {
  g.__tallyOnboarding = freshState();
}
