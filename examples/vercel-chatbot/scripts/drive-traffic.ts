// SPDX-License-Identifier: Apache-2.0
// Synthetic traffic driver for the chatbot demo.
//
// SCRIPTED SESSIONS, NOT REAL USERS. This file deliberately uses a seeded RNG
// and a fixed prompt set so two demo runs at the same `--seed` produce the
// same cost + attribution numbers. The point of the demo is to exercise the
// ai-tally workflow-2/3/4 dashboards end-to-end, not to mimic real user
// behavior — anyone reading the README must understand that.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
// ai-tally (CTO-137): import the embedding-span helper directly. The real chat
// route has no embedding/RAG path today, so rather than fabricate one we have a
// fraction of synthetic sessions emit a RAG-retrieval embedding span straight
// to the gateway. This is DEMO SEED TRAFFIC — clearly not a real code path.
import { postEmbeddingSpan } from "../app/lib/tally";

// Embedding pricing for the simulated RAG retrieval. text-embedding-3-small is
// $0.02 per 1M input tokens. Demo-seed only — the gateway catalog remains the
// source of truth for any real numbers.
const EMBED_MODEL = "text-embedding-3-small";
const EMBED_USD_PER_MTOK = 0.02;

function embedCostMicroUsd(inputTokens: number): number {
  return Math.round(((inputTokens * EMBED_USD_PER_MTOK) / 1_000_000) * 1_000_000);
}

interface Prompt {
  id: string;
  tag: "chatbot.support" | "chatbot.brainstorm" | "chatbot.code";
  opener: string;
  followups: string[];
}

type Mode = "quick" | "realistic";

interface Args {
  mode: Mode;
  sessions: number;
  conversionRate: number;
  provider: "openai" | "anthropic" | "mixed";
  seed: number;
  chatbotUrl: string;
  dryRun: boolean;
  parallel: number;
  /** Realistic mode: spread sessions over this many minutes (0 = as fast as possible). */
  windowMin: number;
  /** Realistic mode: hard cap on estimated live API spend (USD); 0 = no cap. */
  maxUsd: number;
}

// ai-tally (CTO-138): realistic-volume mode drives a startup-scale run so a live
// `make chatbot-demo MODE=realistic` matches the seed story ($52,400/mo) instead
// of a fraction-of-a-cent quick run. Sessions are tagged across the seed feature
// mix and conversions fire at per-provider rates from the attribution screenshot.
const REALISTIC_DEFAULTS = {
  sessions: 5000,
  windowMin: 10,
  maxUsd: 10,
  parallel: 12,
};

// Feature mix for realistic mode — share of sessions. Matches the seed fixtures
// in web/lib/mock.ts. The chatbot route classifies prompts into chatbot.* tags,
// so we additionally pass these as the run's higher-level feature label.
const REALISTIC_FEATURE_MIX: { tag: string; share: number }[] = [
  { tag: "research_agent", share: 0.54 },
  { tag: "support_triage", share: 0.17 },
  { tag: "inline_writer", share: 0.12 },
  { tag: "smart_search", share: 0.1 },
  { tag: "chatbot", share: 0.07 },
];

// Per-provider conversion rates (attribution screenshot): 13% openai / 15% anthropic.
const REALISTIC_CONVERSION = { openai: 0.13, anthropic: 0.15 };
const REALISTIC_POSITIVE_FEEDBACK = 0.75;

function parseArgs(argv: string[]): Args {
  const envMode = (process.env.MODE ?? "").toLowerCase();
  const mode: Mode = envMode === "realistic" ? "realistic" : "quick";
  const defaults: Args = {
    mode,
    sessions: 50,
    conversionRate: 0.2,
    provider: "mixed",
    seed: 42,
    chatbotUrl: process.env.TALLY_CHATBOT_URL ?? "http://localhost:3001",
    dryRun: false,
    parallel: 4,
    windowMin: 0,
    maxUsd: 0,
  };
  const out = { ...defaults };
  // Track whether the operator explicitly set a knob so --mode=realistic can
  // apply its scaled defaults without clobbering explicit overrides.
  const explicit = new Set<string>();
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = argv[i + 1];
    const eq = a.indexOf("=");
    const flag = eq === -1 ? a : a.slice(0, eq);
    const inlineVal = eq === -1 ? undefined : a.slice(eq + 1);
    const take = (): string => {
      if (inlineVal !== undefined) return inlineVal;
      i++;
      return next;
    };
    switch (flag) {
      case "--mode": {
        const v = take().toLowerCase();
        if (v !== "quick" && v !== "realistic") {
          throw new Error("--mode must be quick|realistic");
        }
        out.mode = v;
        break;
      }
      case "--sessions":
        out.sessions = parseInt(take(), 10);
        explicit.add("sessions");
        break;
      case "--conversion-rate":
        out.conversionRate = parseFloat(take());
        explicit.add("conversionRate");
        break;
      case "--provider": {
        const v = take();
        if (v !== "openai" && v !== "anthropic" && v !== "mixed") {
          throw new Error("--provider must be openai|anthropic|mixed");
        }
        out.provider = v;
        break;
      }
      case "--seed":
        out.seed = parseInt(take(), 10);
        break;
      case "--url":
        out.chatbotUrl = take();
        break;
      case "--dry-run":
        out.dryRun = true;
        break;
      case "--parallel":
        out.parallel = Math.max(1, parseInt(take(), 10));
        explicit.add("parallel");
        break;
      case "--window-min":
        out.windowMin = Math.max(0, parseFloat(take()));
        explicit.add("windowMin");
        break;
      case "--max-usd":
        out.maxUsd = Math.max(0, parseFloat(take()));
        explicit.add("maxUsd");
        break;
      case "--help":
      case "-h":
        console.log(
          "usage: tsx drive-traffic.ts [--mode quick|realistic] [--sessions N] " +
            "[--conversion-rate 0..1] [--provider openai|anthropic|mixed] [--seed N] " +
            "[--url http://...] [--dry-run] [--parallel N] [--window-min M] [--max-usd U]\n" +
            "  quick (default): 50 scripted sessions, ~$0.40.\n" +
            "  realistic: ~5000 sessions over ~10 min, capped at --max-usd (default $10).",
        );
        process.exit(0);
        break;
    }
  }
  // Apply realistic-mode scaled defaults for any knob the operator didn't set.
  if (out.mode === "realistic") {
    if (!explicit.has("sessions")) out.sessions = REALISTIC_DEFAULTS.sessions;
    if (!explicit.has("windowMin")) out.windowMin = REALISTIC_DEFAULTS.windowMin;
    if (!explicit.has("maxUsd")) out.maxUsd = REALISTIC_DEFAULTS.maxUsd;
    if (!explicit.has("parallel")) out.parallel = REALISTIC_DEFAULTS.parallel;
  }
  if (!Number.isFinite(out.sessions) || out.sessions <= 0) {
    throw new Error("--sessions must be a positive integer");
  }
  if (out.conversionRate < 0 || out.conversionRate > 1) {
    throw new Error("--conversion-rate must be in [0, 1]");
  }
  return out;
}

// Mulberry32 — small, deterministic PRNG. Plenty good for picking prompts.
function makeRng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick<T>(rng: () => number, arr: T[]): T {
  return arr[Math.floor(rng() * arr.length)];
}

function loadPrompts(): Prompt[] {
  const p = path.resolve(path.dirname(new URL(import.meta.url).pathname), "prompts.json");
  return JSON.parse(fs.readFileSync(p, "utf-8")) as Prompt[];
}

interface SessionResult {
  sessionId: string;
  provider: "openai" | "anthropic";
  tag: string;
  turns: number;
  inputTokens: number;
  outputTokens: number;
  costMicroUsd: number;
  converted: boolean;
  errors: number;
}

// Rough driver-side cost estimate for the run summary printed at the end of
// `drive-traffic`. The gateway's catalog (CTO-106) computes authoritative cost
// from real provider+model+tokens; ClickHouse remains the source of truth.
// Rates here are a coarse blended average and are not used for any dashboard
// number — only the local summary line.
const SUMMARY_BLENDED_INPUT_PER_MTOK = 1.5;
const SUMMARY_BLENDED_OUTPUT_PER_MTOK = 8.0;

function estimatedSummaryCostMicroUsd(inputTok: number, outputTok: number): number {
  return Math.round(
    ((inputTok * SUMMARY_BLENDED_INPUT_PER_MTOK) / 1_000_000 +
      (outputTok * SUMMARY_BLENDED_OUTPUT_PER_MTOK) / 1_000_000) *
      1_000_000,
  );
}

interface ChatResponse {
  reply: string;
  inputTokens: number;
  outputTokens: number;
  featureTag: string;
}

async function postChat(
  args: Args,
  sessionId: string,
  prompt: string,
  provider: "openai" | "anthropic",
  turnIndex: number,
  featureTag: string,
): Promise<ChatResponse> {
  const res = await fetch(`${args.chatbotUrl}/api/demo-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sessionId,
      prompt,
      provider,
      turnIndex,
      featureTag,
      dryRun: args.dryRun,
    }),
  });
  if (!res.ok) {
    throw new Error(`POST /api/demo-chat ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as ChatResponse;
}

async function postEvent(
  args: Args,
  sessionId: string,
  type: "positive_feedback" | "conversion",
  featureTag: string,
): Promise<void> {
  const res = await fetch(`${args.chatbotUrl}/api/demo-event`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, type, featureTag }),
  });
  if (!res.ok) {
    throw new Error(`POST /api/demo-event ${res.status}: ${await res.text()}`);
  }
}

function pickFeatureTag(rng: () => number): string {
  const r = rng();
  let acc = 0;
  for (const f of REALISTIC_FEATURE_MIX) {
    acc += f.share;
    if (r <= acc) return f.tag;
  }
  return REALISTIC_FEATURE_MIX[REALISTIC_FEATURE_MIX.length - 1].tag;
}

async function runSession(
  args: Args,
  rng: () => number,
  prompts: Prompt[],
  index: number,
): Promise<SessionResult> {
  const realistic = args.mode === "realistic";
  const sessionId = `chatbot-demo-${args.seed}-${index}-${crypto
    .randomBytes(4)
    .toString("hex")}`;
  const prompt = pick(rng, prompts);
  // Realistic mode labels each session with a seed-mix feature tag; quick mode
  // keeps the prompt's intrinsic chatbot.* classification.
  const featureTag = realistic ? pickFeatureTag(rng) : prompt.tag;
  const provider: "openai" | "anthropic" =
    args.provider === "mixed" ? (rng() < 0.5 ? "openai" : "anthropic") : args.provider;
  const turns = 3 + Math.floor(rng() * 6); // 3..8 turns inclusive
  let inputTokens = 0;
  let outputTokens = 0;
  let errors = 0;

  const messages = [prompt.opener, ...prompt.followups].slice(0, turns);
  for (let t = 0; t < messages.length; t++) {
    try {
      const r = await postChat(
        args,
        sessionId,
        messages[t],
        provider,
        t,
        featureTag,
      );
      inputTokens += r.inputTokens;
      outputTokens += r.outputTokens;
    } catch (err) {
      errors++;
      console.warn(`[session ${index}] turn ${t} failed: ${(err as Error).message}`);
      break;
    }
  }

  // ai-tally (CTO-137): ~30% of sessions ALSO emit a simulated RAG-retrieval
  // embedding span so the Cost tab's Embeddings bar is non-zero on a live demo.
  // DEMO SEED TRAFFIC — the real chat route has no embedding path; this stands
  // in for a retrieval step. Best-effort; never fails the session.
  if (rng() < 0.3 && errors === 0 && !args.dryRun) {
    const embedTokens = 200 + Math.floor(rng() * 600); // 200..799
    try {
      await postEmbeddingSpan({
        sessionId,
        provider: "openai",
        model: EMBED_MODEL,
        inputTokens: embedTokens,
        costMicroUsd: embedCostMicroUsd(embedTokens),
        runId: sessionId,
      });
    } catch (err) {
      console.warn(
        `[session ${index}] embedding span failed: ${(err as Error).message}`,
      );
    }
  }

  // positive_feedback signal regardless of conversion — this is what the
  // workflow-4 dashboard counts when filtered by outcome=positive_feedback.
  // Quick mode keeps the original ~30%; realistic mode fires ~75% to match the
  // attribution screenshot.
  const feedbackRate = realistic ? REALISTIC_POSITIVE_FEEDBACK : 0.3;
  if (rng() < feedbackRate && errors === 0) {
    try {
      await postEvent(args, sessionId, "positive_feedback", featureTag);
    } catch (err) {
      console.warn(`[session ${index}] positive_feedback failed: ${(err as Error).message}`);
    }
  }

  // Hard conversion (monetary). Quick mode uses the flat --conversion-rate;
  // realistic mode uses per-provider rates (13% openai / 15% anthropic) from
  // the attribution screenshot.
  const convThreshold = realistic
    ? REALISTIC_CONVERSION[provider]
    : args.conversionRate;
  const converted = rng() < convThreshold && errors === 0;
  if (converted) {
    try {
      await postEvent(args, sessionId, "conversion", featureTag);
    } catch (err) {
      console.warn(`[session ${index}] conversion failed: ${(err as Error).message}`);
    }
  }

  return {
    sessionId,
    provider,
    tag: featureTag,
    turns: messages.length,
    inputTokens,
    outputTokens,
    costMicroUsd: estimatedSummaryCostMicroUsd(inputTokens, outputTokens),
    converted,
    errors,
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function runAll(args: Args): Promise<SessionResult[]> {
  const rng = makeRng(args.seed);
  const prompts = loadPrompts();
  const results: SessionResult[] = [];
  let nextIndex = 0;
  let spentMicroUsd = 0;
  let capped = false;

  // Realistic mode paces sessions across a bounded window so the dashboard
  // shows a live-looking ramp instead of a single instant spike. The delay is
  // the average gap between session starts given the target session count.
  const perSessionDelayMs =
    args.mode === "realistic" && args.windowMin > 0
      ? (args.windowMin * 60_000) / args.sessions
      : 0;
  const maxMicroUsd = args.maxUsd > 0 ? args.maxUsd * 1_000_000 : 0;
  const t0 = Date.now();

  async function worker(): Promise<void> {
    while (true) {
      // --max-usd cap: stop launching new sessions once the estimated live
      // spend crosses the cap. Keeps a laptop realistic run bounded (~$5-10).
      if (maxMicroUsd > 0 && spentMicroUsd >= maxMicroUsd) {
        capped = true;
        return;
      }
      const i = nextIndex++;
      if (i >= args.sessions) return;
      if (perSessionDelayMs > 0) {
        // Pace against wall-clock so the whole run spreads across the window
        // even as workers finish at different speeds.
        const targetElapsed = i * perSessionDelayMs;
        const drift = targetElapsed - (Date.now() - t0);
        if (drift > 0) await sleep(drift);
      }
      const r = await runSession(args, rng, prompts, i);
      results.push(r);
      spentMicroUsd += r.costMicroUsd;
      if (results.length % 100 === 0) {
        console.log(
          `  · ${results.length} sessions done (est. spend ${fmtUsd(spentMicroUsd)})`,
        );
      }
    }
  }

  const workers = Array.from(
    { length: Math.min(args.parallel, args.sessions) },
    () => worker(),
  );
  await Promise.all(workers);
  if (capped) {
    console.log(
      `  · --max-usd cap (${fmtUsd(maxMicroUsd)}) reached — stopped after ${results.length} sessions.`,
    );
  }
  return results;
}

function fmtUsd(micro: number): string {
  const usd = micro / 1_000_000;
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const t0 = Date.now();
  if (args.mode === "realistic") {
    console.log(
      `Driving up to ${args.sessions} synthetic sessions in REALISTIC mode ` +
        `(provider=${args.provider}, seed=${args.seed}, window=${args.windowMin}min, ` +
        `max-usd=${args.maxUsd || "∞"})…`,
    );
    console.log(
      "  NOTE: scripted sessions, not real users. This path makes REAL LLM " +
        "calls — spend is capped by --max-usd. For $0 screenshot data, use the " +
        "backfill script instead (make chatbot-demo-backfill).",
    );
  } else {
    console.log(
      `Driving ${args.sessions} synthetic sessions ` +
        `(provider=${args.provider}, conversion=${args.conversionRate}, seed=${args.seed})…`,
    );
    console.log("  NOTE: these are scripted sessions, not real users.");
  }

  const results = await runAll(args);
  const elapsedS = Math.round((Date.now() - t0) / 1000);

  const totalCost = results.reduce((s, r) => s + r.costMicroUsd, 0);
  const conversions = results.filter((r) => r.converted).length;
  const errored = results.filter((r) => r.errors > 0).length;

  console.log("");
  console.log(
    `✓ Done. ${results.length} sessions ingested in ${elapsedS}s. ` +
      `Total cost: ${fmtUsd(totalCost)}. ` +
      `Conversions: ${conversions}/${results.length} ` +
      `(${Math.round((conversions / results.length) * 100)}%).`,
  );
  if (errored > 0) {
    console.log(`  · ${errored} session(s) hit errors — see warnings above.`);
  }
}

main().catch((err) => {
  console.error("drive-traffic failed:", err);
  process.exit(1);
});
