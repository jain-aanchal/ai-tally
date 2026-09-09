// SPDX-License-Identifier: Apache-2.0
// Server-only in-memory onboarding store backing the activation funnel sink, keyed by tenant.
//
// In production these are control-plane rows. This is still the prototype's in-process stand-in, so
// state is per PROCESS and is lost on restart and on every deploy, and two web instances behind a
// load balancer hold two different answers. That is a real limitation and it is written here rather
// than implied: the funnel is a UI progress aid, not a source of truth anything else reads.
//
// #358: what it is NOT any more is a single record shared by everyone. It used to be one
// `globalThis.__tallyOnboarding`, so on a multi-tenant deployment every organization saw and
// overwrote the same onboarding progress. Keying by the resolved tenant UUID (the canonical
// identifier, per CLAUDE.md) makes the isolation match the rest of the product; the honest caveat
// above is what is left, and it is a deployment-lifetime caveat rather than a correctness bug.
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
      copiedConfigAt: null,
      firstDashboardAt: null,
    },
    // #358: the funnel starts EMPTY. It used to be seeded with a `signed_up` event stamped
    // Date.now(), which on a process-global store meant server boot time and on a per-tenant store
    // would mean the first time this process served the tenant. Neither is a signup, and Clerk owns
    // the only real one. Same rule #329 applied to first-trace: a stage we cannot time is not
    // recorded with a stand-in clock reading. The checklist's first step does not depend on it.
    funnel: [],
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

// Survive Next.js dev hot-reload by stashing on globalThis. One entry per tenant UUID.
const g = globalThis as unknown as { __tallyOnboarding?: Map<string, StoreState> };
function state(tenantId: string): StoreState {
  if (!g.__tallyOnboarding) g.__tallyOnboarding = new Map<string, StoreState>();
  let s = g.__tallyOnboarding.get(tenantId);
  if (!s) {
    s = freshState();
    g.__tallyOnboarding.set(tenantId, s);
  }
  return s;
}

export function getProgress(tenantId: string): OnboardingProgress {
  return { ...state(tenantId).progress };
}

export function getCreds(tenantId: string): TenantProxyCredentials {
  return { ...state(tenantId).creds };
}

export function getFunnel(tenantId: string): FunnelEvent[] {
  return [...state(tenantId).funnel];
}

/**
 * Record a funnel stage for one tenant.
 *
 * `noticed` marks a stage we observed after the fact rather than timed (#329). The onboarding page
 * posts first_trace that way, off the coverage probe: the stage is real, the moment is only when we
 * spotted it, so the event carries the flag and is never mirrored onto a progress timestamp that
 * would then be read as a measured duration. A stage is recorded once; later reports are ignored.
 */
export function recordFunnel(
  tenantId: string,
  stage: FunnelStage,
  opts: { noticed?: boolean } = {},
): FunnelEvent {
  const s = state(tenantId);
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
export function hasFunnelStage(tenantId: string, stage: FunnelStage): boolean {
  return state(tenantId).funnel.some((e) => e.stage === stage);
}

/** Test-only: drop every tenant's in-memory record. */
export function __resetOnboarding(): void {
  g.__tallyOnboarding = new Map<string, StoreState>();
}
