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

interface Args {
  sessions: number;
  conversionRate: number;
  provider: "openai" | "anthropic" | "mixed";
  seed: number;
  chatbotUrl: string;
  dryRun: boolean;
  parallel: number;
}

function parseArgs(argv: string[]): Args {
  const defaults: Args = {
    sessions: 50,
    conversionRate: 0.2,
    provider: "mixed",
    seed: 42,
    chatbotUrl: process.env.TALLY_CHATBOT_URL ?? "http://localhost:3001",
    dryRun: false,
    parallel: 4,
  };
  const out = { ...defaults };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = argv[i + 1];
    switch (a) {
      case "--sessions":
        out.sessions = parseInt(next, 10);
        i++;
        break;
      case "--conversion-rate":
        out.conversionRate = parseFloat(next);
        i++;
        break;
      case "--provider":
        if (next !== "openai" && next !== "anthropic" && next !== "mixed") {
          throw new Error("--provider must be openai|anthropic|mixed");
        }
        out.provider = next;
        i++;
        break;
      case "--seed":
        out.seed = parseInt(next, 10);
        i++;
        break;
      case "--url":
        out.chatbotUrl = next;
        i++;
        break;
      case "--dry-run":
        out.dryRun = true;
        break;
      case "--parallel":
        out.parallel = Math.max(1, parseInt(next, 10));
        i++;
        break;
      case "--help":
      case "-h":
        console.log(
          "usage: tsx drive-traffic.ts [--sessions N] [--conversion-rate 0..1] " +
            "[--provider openai|anthropic|mixed] [--seed N] [--url http://...] [--dry-run] [--parallel N]",
        );
        process.exit(0);
        break;
    }
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

async function runSession(
  args: Args,
  rng: () => number,
  prompts: Prompt[],
  index: number,
): Promise<SessionResult> {
  const sessionId = `chatbot-demo-${args.seed}-${index}-${crypto
    .randomBytes(4)
    .toString("hex")}`;
  const prompt = pick(rng, prompts);
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
        prompt.tag,
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

  // ~30% of sessions emit a positive_feedback signal regardless of conversion —
  // this is what the workflow-4 dashboard counts when filtered by
  // outcome=positive_feedback.
  if (rng() < 0.3 && errors === 0) {
    try {
      await postEvent(args, sessionId, "positive_feedback", prompt.tag);
    } catch (err) {
      console.warn(`[session ${index}] positive_feedback failed: ${(err as Error).message}`);
    }
  }

  // Hard conversion (monetary) driven off --conversion-rate.
  const converted = rng() < args.conversionRate && errors === 0;
  if (converted) {
    try {
      await postEvent(args, sessionId, "conversion", prompt.tag);
    } catch (err) {
      console.warn(`[session ${index}] conversion failed: ${(err as Error).message}`);
    }
  }

  return {
    sessionId,
    provider,
    tag: prompt.tag,
    turns: messages.length,
    inputTokens,
    outputTokens,
    costMicroUsd: estimatedSummaryCostMicroUsd(inputTokens, outputTokens),
    converted,
    errors,
  };
}

async function runAll(args: Args): Promise<SessionResult[]> {
  const rng = makeRng(args.seed);
  const prompts = loadPrompts();
  const results: SessionResult[] = new Array(args.sessions);
  let nextIndex = 0;

  async function worker(): Promise<void> {
    while (true) {
      const i = nextIndex++;
      if (i >= args.sessions) return;
      results[i] = await runSession(args, rng, prompts, i);
      if ((i + 1) % 10 === 0) {
        console.log(`  · ${i + 1}/${args.sessions} sessions done`);
      }
    }
  }

  const workers = Array.from({ length: Math.min(args.parallel, args.sessions) }, () =>
    worker(),
  );
  await Promise.all(workers);
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
  console.log(
    `Driving ${args.sessions} synthetic sessions ` +
      `(provider=${args.provider}, conversion=${args.conversionRate}, seed=${args.seed})…`,
  );
  console.log("  NOTE: these are scripted sessions, not real users.");

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
