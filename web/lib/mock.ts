// SPDX-License-Identifier: Apache-2.0
// Demo seed fixtures for the UI. Typed identically to the eventual API responses so wiring real
// endpoints later is a drop-in replacement.
//
// Storyline: a YC-stage SaaS spending ~$52K/mo on LLMs across five real-feeling AI features,
// with two providers in production (OpenAI + Anthropic) and Stripe revenue attributed back.
// Numbers chosen to be believable for screenshots / a LinkedIn demo, not real telemetry.

import { isDemoMode } from "./demoMode";
import type { CostOutlier, DataQuality, FeatureRoi, SpendSummary } from "./types";

/**
 * The one gate every fixture fallback in `app/api/**` passes through (#363).
 *
 * Fixtures used to be the answer to "the read failed" AND to "the read came back empty", so a real
 * signed-in customer with zero spans was shown this file's storyline as their own numbers: a
 * research_agent they have never run, paying back in 7 days. That is the honesty invariant's
 * headline failure, and no banner makes it acceptable, so the fixtures are now unreachable on the
 * product path rather than merely labelled there.
 *
 * TWO conditions, and both are load-bearing:
 *
 *   NEXT_PUBLIC_DEMO_MODE=1  an explicit, deliberate opt-in. Nobody sets it by accident, and it is
 *                            already the flag a demo build sets for the rest of its presentation.
 *   TALLY_DEV_TENANT set     no Clerk organization was resolved. This is the ONLY way the dashboard
 *                            serves a tenant without an authenticated org (see lib/getTenant.ts), so
 *                            requiring it makes "a real tenant is signed in" and "fixtures render"
 *                            mutually exclusive by construction, not by review vigilance.
 *
 * Deliberately NOT read here: whether ClickHouse is configured. An unreachable store is a reason to
 * say so, not a licence to invent numbers, which is what `deploy/vercel/README.md` used to document.
 *
 * Read at call time rather than hoisted to a module constant so a test can set the environment per
 * case; `NEXT_PUBLIC_*` is inlined at build time on the client, and this is only ever called from a
 * Route Handler on the server, where both values are live.
 */
export function sampleDataAllowed(): boolean {
  return isDemoMode() && Boolean(process.env.TALLY_DEV_TENANT?.trim());
}

export const mockSpend: SpendSummary = {
  totalMicroUsd: 52_400_000_000, // $52,400 over the last 30 days
  estimatedMicroUsd: 27_180_000_000, // ~52% still estimated (recent days, not yet reconciled)
  reconciledMicroUsd: 25_220_000_000, // ~48% reconciled
  reconciledThrough: "2026-06-12",
  byLayer: {
    llm: 38_200_000_000, // 73%, the bulk
    vector: 6_800_000_000, // 13%
    tools: 4_100_000_000, // 8%
    compute: 2_400_000_000, // 4.5%
    embeddings: 700_000_000, // 1.3%
    egress: 200_000_000, // 0.4%
  },
};

export const mockOutliers: CostOutlier[] = [
  { runId: "research_run_8af2", agent: "research_agent", costMicroUsd: 4_870_000, multipleOfMedian: 41 },
  { runId: "research_run_3c01", agent: "research_agent", costMicroUsd: 3_120_000, multipleOfMedian: 26 },
  { runId: "chat_completion_99fb", agent: "smart_search", costMicroUsd: 890_000, multipleOfMedian: 22 },
  { runId: "research_run_7b3a", agent: "research_agent", costMicroUsd: 760_000, multipleOfMedian: 6 },
  { runId: "writer_run_2f1c", agent: "inline_writer", costMicroUsd: 540_000, multipleOfMedian: 14 },
];

export const mockRoi: FeatureRoi[] = [
  { feature: "research_agent", costPerUserMicroUsd: 320_000, valuePerUserMicroUsd: 4_200_000, paybackDays: 7, attributionRate: 0.91 },
  { feature: "support_triage", costPerUserMicroUsd: 12_000, valuePerUserMicroUsd: 310_000, paybackDays: 1, attributionRate: 0.79 },
  { feature: "inline_writer", costPerUserMicroUsd: 80_000, valuePerUserMicroUsd: 450_000, paybackDays: 5, attributionRate: 0.88 },
  { feature: "chatbot", costPerUserMicroUsd: 67_000, valuePerUserMicroUsd: 220_000, paybackDays: 9, attributionRate: 0.73 },
  { feature: "smart_search", costPerUserMicroUsd: 40_000, valuePerUserMicroUsd: 180_000, paybackDays: 7, attributionRate: 0.82 },
  { feature: "summarize", costPerUserMicroUsd: 5_000, valuePerUserMicroUsd: null, paybackDays: null, attributionRate: null },
];

export const mockDataQuality: DataQuality = {
  attributionRate: 0.84, // 84% of business events tied back to a trace
  contextDropCount: 3, // a handful of context-window drops, surfaced not hidden
  estimateCalibration: 0.018, // 1.8% off the reconciled period
};
