// Guardrail config model + helpers for the config UI (CTO-58). Typed like the eventual control-plane
// API (Postgres, CTO-27); the SDK guardrail engine (CTO-51) consumes the same shapes.
//
// The product bet: customers graduate to enforcement *with confidence*. Every rule starts in
// observe-only, where the engine records what *would* have fired without touching agent behavior.
// The "would-have-fired this week" count is the graduation signal — once you can see the blast
// radius of a cap, flipping it to warn/graceful is a low-risk decision.

import type { MicroUSD } from "./types";

// Mirrors tally.guardrails.Mode (observe / warn / graceful / hard_stop).
export type GuardrailMode = "observe" | "warn" | "graceful" | "hard_stop";

export interface GuardrailModeMeta {
  mode: GuardrailMode;
  label: string;
  blurb: string;
  /** true = the agent's behavior is altered (enforcing); observe never alters it */
  enforcing: boolean;
}

// Ordered weakest → strongest. Order is load-bearing for the graduation ladder.
export const GUARDRAIL_MODES: GuardrailModeMeta[] = [
  {
    mode: "observe",
    label: "Observe-only",
    blurb: "Record what would have fired. Never alters the agent. Safe to leave on forever.",
    enforcing: false,
  },
  {
    mode: "warn",
    label: "Warn",
    blurb: "Proceed, but inject a budget warning the agent can act on (e.g. converge).",
    enforcing: true,
  },
  {
    mode: "graceful",
    label: "Graceful stop",
    blurb: "Raise a catchable limit so the framework cleans up and returns a degraded response.",
    enforcing: true,
  },
  {
    mode: "hard_stop",
    label: "Hard stop",
    blurb: "Abort the call outright. Opt-in only — for idempotent/read-only agents.",
    enforcing: true,
  },
];

export function modeMeta(mode: GuardrailMode): GuardrailModeMeta {
  return GUARDRAIL_MODES.find((m) => m.mode === mode) ?? GUARDRAIL_MODES[0];
}

export type GuardrailScopeKind = "agent" | "feature";

export interface GuardrailRule {
  id: string;
  scopeKind: GuardrailScopeKind;
  /** the agent name or feature tag this rule applies to */
  scope: string;
  mode: GuardrailMode;
  /** per-run cost cap; null = no cost cap on this rule */
  maxCostMicroUsd: MicroUSD | null;
  /** per-run step cap; null = no step cap on this rule */
  maxSteps: number | null;
  /** how many runs in the last 7d would have tripped this rule's caps */
  wouldHaveFiredThisWeek: number;
  /** total runs observed in the last 7d for this scope (denominator for the rate) */
  runsThisWeek: number;
}

// The SDK polls the control plane for config on this cadence, so a mode change takes effect within
// this window (AC: "changing mode takes effect on the SDK within the config-refresh window").
export const CONFIG_REFRESH_SECONDS = 60;

/** Fraction of this week's runs that would have fired (0..1). 0 when no runs observed. */
export function fireRate(rule: GuardrailRule): number {
  if (rule.runsThisWeek <= 0) return 0;
  return rule.wouldHaveFiredThisWeek / rule.runsThisWeek;
}

export type GraduationSignal = "ready" | "review" | "noisy" | "insufficient-data";

// The graduation heuristic the UI surfaces for observe-only rules:
//  - insufficient-data: too few runs to judge the blast radius yet.
//  - ready:  a small, non-zero fraction would fire — a meaningful but contained cap. Graduate.
//  - review: nothing would fire — the cap may be set too loose to matter.
//  - noisy:  a large fraction would fire — enforcing now would disrupt many runs; tune the cap first.
export function graduationSignal(rule: GuardrailRule): GraduationSignal {
  if (rule.runsThisWeek < 100) return "insufficient-data";
  const rate = fireRate(rule);
  if (rate === 0) return "review";
  if (rate <= 0.05) return "ready";
  return "noisy";
}

export const GRADUATION_LABEL: Record<GraduationSignal, string> = {
  ready: "Ready to enforce",
  review: "Cap never fires — review",
  noisy: "Too noisy — tune the cap",
  "insufficient-data": "Not enough data yet",
};

/** A rule is well-formed only if it constrains *something* (a cost cap or a step cap). */
export function isActionable(rule: Pick<GuardrailRule, "maxCostMicroUsd" | "maxSteps">): boolean {
  return rule.maxCostMicroUsd !== null || rule.maxSteps !== null;
}

export interface GuardrailSummary {
  total: number;
  enforcing: number;
  observing: number;
  readyToGraduate: number;
}

export function summarize(rules: GuardrailRule[]): GuardrailSummary {
  let enforcing = 0;
  let observing = 0;
  let readyToGraduate = 0;
  for (const r of rules) {
    if (modeMeta(r.mode).enforcing) enforcing += 1;
    else {
      observing += 1;
      if (graduationSignal(r) === "ready") readyToGraduate += 1;
    }
  }
  return { total: rules.length, enforcing, observing, readyToGraduate };
}

// Mock rules — typed exactly like the eventual control-plane response.
export const guardrailRules: GuardrailRule[] = [
  {
    id: "gr_research_cost",
    scopeKind: "agent",
    scope: "research_agent",
    mode: "observe",
    maxCostMicroUsd: 1_000_000, // $1.00 / run
    maxSteps: 30,
    wouldHaveFiredThisWeek: 312,
    runsThisWeek: 8_680,
  },
  {
    id: "gr_support_steps",
    scopeKind: "agent",
    scope: "support_triage",
    mode: "warn",
    maxCostMicroUsd: null,
    maxSteps: 12,
    wouldHaveFiredThisWeek: 1_240,
    runsThisWeek: 58_100,
  },
  {
    id: "gr_inline_cost",
    scopeKind: "feature",
    scope: "inline_writer",
    mode: "graceful",
    maxCostMicroUsd: 20_000, // $0.02 / run
    maxSteps: null,
    wouldHaveFiredThisWeek: 410,
    runsThisWeek: 294_700,
  },
  {
    id: "gr_smartsearch_cost",
    scopeKind: "feature",
    scope: "smart_search",
    mode: "observe",
    maxCostMicroUsd: 5_000_000, // $5.00 / run — likely too loose
    maxSteps: null,
    wouldHaveFiredThisWeek: 0,
    runsThisWeek: 1_900,
  },
];
