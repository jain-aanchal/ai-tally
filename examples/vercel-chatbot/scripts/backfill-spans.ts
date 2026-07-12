// SPDX-License-Identifier: Apache-2.0
// Synthetic 30-day backfill for the chatbot demo.
//
// SCRIPTED SEED DATA, NOT REAL USERS AND NOT REAL API CALLS. This script POSTs
// backdated spans + business_events straight to the ai-tally gateway's
// /v1/batches endpoint. It makes NO LLM calls and needs NO OpenAI/Anthropic
// key — a screenshot run costs $0. The point is to make a freshly-seeded local
// stack match the LinkedIn/seed story (~$52,400/mo, the feature mix from
// web/lib/mock.ts) so the Cost / Attribution dashboards have a month of history
// to render instead of a fraction-of-a-cent live run.
//
// Everything here is synthetic-seed: the timestamps are backdated across the
// prior ~30 days, the token counts are drawn from a seeded RNG, and the
// conversion "revenue" is fabricated. The gateway's enrich_cost still computes
// authoritative cost from (provider, model, tokens) against the seed catalog —
// we only choose token volumes so the totals land on the seed story.
//
// Idempotent: batch_ids are derived deterministically from (seed, batch-index)
// so re-running with the same --seed hits the gateway's (tenant_id, batch_id)
// dedup cache (24h TTL) and does not double-count. Use a fresh --seed to layer
// in a second independent month.

import crypto from "node:crypto";

// ---------------------------------------------------------------------------
// Config / CLI
// ---------------------------------------------------------------------------

interface Args {
  gatewayUrl: string;
  tenant: string;
  seed: number;
  days: number;
  targetUsd: number;
  dryRun: boolean;
}

function parseArgs(argv: string[]): Args {
  const defaults: Args = {
    gatewayUrl:
      process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080/v1/batches",
    tenant: process.env.TALLY_TENANT ?? "local-dev",
    seed: 138,
    days: 30,
    targetUsd: 52_400,
    dryRun: false,
  };
  const out = { ...defaults };
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
      case "--gateway-url":
        out.gatewayUrl = take();
        break;
      case "--tenant":
        out.tenant = take();
        break;
      case "--seed":
        out.seed = parseInt(take(), 10);
        break;
      case "--days":
        out.days = parseInt(take(), 10);
        break;
      case "--target-usd":
        out.targetUsd = parseFloat(take());
        break;
      case "--dry-run":
        out.dryRun = true;
        break;
      case "--help":
      case "-h":
        console.log(
          "usage: tsx backfill-spans.ts [--gateway-url URL] [--tenant T] " +
            "[--seed N] [--days N] [--target-usd 52400] [--dry-run]\n" +
            "  Posts backdated synthetic spans + conversions to the gateway. " +
            "No LLM calls, no API key, $0.",
        );
        process.exit(0);
    }
  }
  if (!Number.isFinite(out.days) || out.days <= 0) {
    throw new Error("--days must be a positive integer");
  }
  if (!Number.isFinite(out.targetUsd) || out.targetUsd <= 0) {
    throw new Error("--target-usd must be a positive number");
  }
  return out;
}

// Mulberry32 — same deterministic PRNG the live driver uses.
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

function hexId(bytes: number): string {
  return crypto.randomBytes(bytes).toString("hex");
}

// Deterministic batch_id so re-runs at the same --seed dedup on the gateway.
function batchId(seed: number, n: number): string {
  return crypto
    .createHash("sha256")
    .update(`tally-backfill:${seed}:${n}`)
    .digest("hex")
    .slice(0, 32);
}

// Deterministic 64-char user hash for a synthetic user bucket.
function userHash(seed: number, n: number): string {
  return crypto
    .createHash("sha256")
    .update(`tally-backfill-user:${seed}:${n}`)
    .digest("hex");
}

// ---------------------------------------------------------------------------
// Seed story: feature mix, provider mix, model catalog (mirrors
// sdk/python/src/tally/pricing.py seed rates so we can size token volumes to
// the dollar target locally; the gateway re-derives authoritative cost).
// ---------------------------------------------------------------------------

// Feature mix — share of total spend. Matches the seed fixtures in
// web/lib/mock.ts ($52,400/mo YC-stage startup story).
const FEATURE_MIX: { tag: string; share: number }[] = [
  { tag: "research_agent", share: 0.54 },
  { tag: "support_triage", share: 0.17 },
  { tag: "inline_writer", share: 0.12 },
  { tag: "smart_search", share: 0.1 },
  { tag: "chatbot", share: 0.07 },
];

// Provider mix — 60% openai / 40% anthropic.
const OPENAI_SHARE = 0.6;

// Catalog-recognized chat models (USD per million tokens) — these MUST match
// the seed catalog so enrich_cost prices them. Values mirror
// sdk/python/src/tally/pricing.py:seed_catalog (seed-2026-06-15).
interface ChatModel {
  provider: "openai" | "anthropic";
  model: string;
  inUsdPerMtok: number;
  outUsdPerMtok: number;
}
const OPENAI_MODELS: ChatModel[] = [
  { provider: "openai", model: "gpt-4o", inUsdPerMtok: 2.5, outUsdPerMtok: 10.0 },
  {
    provider: "openai",
    model: "gpt-4o-mini",
    inUsdPerMtok: 0.15,
    outUsdPerMtok: 0.6,
  },
];
const ANTHROPIC_MODELS: ChatModel[] = [
  {
    provider: "anthropic",
    model: "claude-sonnet-4-5",
    inUsdPerMtok: 3.0,
    outUsdPerMtok: 15.0,
  },
  {
    provider: "anthropic",
    model: "claude-haiku-4-5",
    inUsdPerMtok: 1.0,
    outUsdPerMtok: 5.0,
  },
];

// Embeddings (openai text-embedding-3-small, $0.02/Mtok) — seed catalog.
const EMBED_MODEL = "text-embedding-3-small";
const EMBED_USD_PER_MTOK = 0.02;

// Tool price table — mirrors the chatbot routes' fixed demo-seed table.
const TOOL_COST_MICRO_USD: Record<string, number> = {
  getWeather: 1_000,
  createDocument: 5_000,
  updateDocument: 5_000,
  requestSuggestions: 2_000,
};
const TOOL_NAMES = Object.keys(TOOL_COST_MICRO_USD);

// Tool + embedding layers. These are micro-priced ($0.001-$0.005/tool span,
// $0.02/Mtok for embeddings), so sizing them as a dollar *share* of $52,400
// would need millions of spans — impractical to POST and unlike the seed mock,
// where LLM dominates and Tools/Embeddings are just small non-zero bars. So the
// LLM layer carries the full headline dollar story and we emit a bounded COUNT
// of tool/embedding spans (~8% / ~1.5% of total spans) purely so those Cost-tab
// bars render non-zero. Span-count proportions, not dollar proportions.
const TOOL_SPAN_SHARE = 0.08;
const EMBED_SPAN_SHARE = 0.015;

// Conversion rates per provider (matches the attribution screenshot).
const CONVERSION_RATE = { openai: 0.13, anthropic: 0.15 };
const POSITIVE_FEEDBACK_RATE = 0.75;
// Synthetic conversion value range (micro-USD): $40–$240.
const CONVERSION_MIN_MICRO = 40_000_000;
const CONVERSION_MAX_MICRO = 200_000_000;

function chatCostUsd(m: ChatModel, inTok: number, outTok: number): number {
  return (
    (inTok * m.inUsdPerMtok) / 1_000_000 +
    (outTok * m.outUsdPerMtok) / 1_000_000
  );
}

// ---------------------------------------------------------------------------
// Span / event builders (same wire shape as app/lib/tally.ts helpers).
// ---------------------------------------------------------------------------

function chatSpan(
  tsNs: number,
  sessionId: string,
  uHash: string,
  m: ChatModel,
  inTok: number,
  outTok: number,
  featureTag: string,
): Record<string, unknown> {
  return {
    ServiceName: "vercel-chatbot",
    SpanName: "chat.completion",
    trace_id: hexId(16),
    span_id: hexId(8),
    timestamp_ns: tsNs,
    duration_ns: 0,
    status_code: 1,
    "gen_ai.system": m.provider,
    "gen_ai.request.model": m.model,
    "gen_ai.response.model": m.model,
    "gen_ai.operation.name": "chat",
    "gen_ai.usage.input_tokens": inTok,
    "gen_ai.usage.output_tokens": outTok,
    "gen_ai.feature_tag": featureTag,
    "gen_ai.session_id": sessionId,
    "gen_ai.user_id_hash": uHash,
    "chatbot.run_id": "backfill",
  };
}

function toolSpan(
  tsNs: number,
  sessionId: string,
  uHash: string,
  provider: string,
  tool: string,
  featureTag: string,
): Record<string, unknown> {
  return {
    ServiceName: "vercel-chatbot",
    SpanName: "tool.execution",
    trace_id: hexId(16),
    span_id: hexId(8),
    timestamp_ns: tsNs,
    duration_ns: 0,
    status_code: 1,
    "gen_ai.system": provider,
    "gen_ai.operation.name": "tool",
    "gen_ai.tool.name": tool,
    "gen_ai.tool.cost_micro_usd": TOOL_COST_MICRO_USD[tool],
    "gen_ai.feature_tag": featureTag,
    "gen_ai.session_id": sessionId,
    "gen_ai.user_id_hash": uHash,
    "chatbot.run_id": "backfill",
  };
}

function embeddingSpan(
  tsNs: number,
  sessionId: string,
  uHash: string,
  inTok: number,
  featureTag: string,
): Record<string, unknown> {
  const costMicro = Math.round(
    ((inTok * EMBED_USD_PER_MTOK) / 1_000_000) * 1_000_000,
  );
  return {
    ServiceName: "vercel-chatbot",
    SpanName: "embeddings",
    trace_id: hexId(16),
    span_id: hexId(8),
    timestamp_ns: tsNs,
    duration_ns: 0,
    status_code: 1,
    "gen_ai.system": "openai",
    "gen_ai.operation.name": "embeddings",
    "gen_ai.request.model": EMBED_MODEL,
    "gen_ai.usage.input_tokens": inTok,
    "gen_ai.cost.estimated_micro_usd": costMicro,
    "gen_ai.feature_tag": featureTag,
    "gen_ai.session_id": sessionId,
    "gen_ai.user_id_hash": uHash,
    "chatbot.run_id": "backfill",
  };
}

function conversionEvent(
  occurredNs: number,
  uHash: string,
  valueMicro: number,
): Record<string, unknown> {
  return {
    business_event_id: crypto.randomUUID(),
    event_name: "conversion",
    user_id_hash: uHash,
    occurred_at_ns: occurredNs,
    value_amount_micro: valueMicro,
    value_currency: "USD",
    value_type: "monetary",
    source: "vercel-chatbot-backfill",
  };
}

function feedbackEvent(
  occurredNs: number,
  uHash: string,
): Record<string, unknown> {
  return {
    business_event_id: crypto.randomUUID(),
    event_name: "positive_feedback",
    user_id_hash: uHash,
    occurred_at_ns: occurredNs,
    value_amount_micro: null,
    value_currency: "USD",
    value_type: "count",
    source: "vercel-chatbot-backfill",
  };
}

// ---------------------------------------------------------------------------
// Generation
// ---------------------------------------------------------------------------

interface Generated {
  spans: Record<string, unknown>[];
  events: Record<string, unknown>[];
  llmCostUsd: number;
  toolCostUsd: number;
  embedCostUsd: number;
  conversions: number;
  feedback: number;
}

function generate(args: Args): Generated {
  const rng = makeRng(args.seed);
  const nowNs = Date.now() * 1_000_000;
  const windowNs = args.days * 24 * 60 * 60 * 1_000_000_000; // days → ns
  const startNs = nowNs - windowNs;

  // Spread a timestamp across the window, with a mild business-hours-ish weight
  // (square the uniform so spend skews toward the recent end — looks alive).
  function randTsNs(): number {
    const u = rng();
    const skew = 1 - (1 - u) * (1 - u); // ease toward 1 (recent)
    return Math.round(startNs + skew * windowNs);
  }

  const spans: Record<string, unknown>[] = [];
  const events: Record<string, unknown>[] = [];
  let llmCostUsd = 0;
  let toolCostUsd = 0;
  let embedCostUsd = 0;
  let conversions = 0;
  let feedback = 0;

  // The LLM layer carries the full dollar story (so the headline matches the
  // seed ~$52,400). Tool/embedding span COUNTS are derived from the LLM span
  // count after generation so the Cost-tab bars are non-zero (see the
  // TOOL_SPAN_SHARE / EMBED_SPAN_SHARE comment above).
  const llmTargetUsd = args.targetUsd;

  let userCounter = 0;
  const nextUser = (): string => userHash(args.seed, userCounter++);

  // --- LLM chat spans, per feature, until each feature hits its $ target -----
  for (const feat of FEATURE_MIX) {
    const featTargetUsd = llmTargetUsd * feat.share;
    let featCostUsd = 0;
    while (featCostUsd < featTargetUsd) {
      // A synthetic session: 1–4 chat turns under one session/user.
      const sessionId = `backfill-${args.seed}-${hexId(4)}`;
      const uHash = nextUser();
      const tsBaseNs = randTsNs();
      const provider = rng() < OPENAI_SHARE ? "openai" : "anthropic";
      const pool = provider === "openai" ? OPENAI_MODELS : ANTHROPIC_MODELS;
      // Bias toward the more capable (pricier) model — pool[0] is gpt-4o /
      // claude-sonnet-4-5. A $52K/mo startup is doing ~100-200k substantial
      // calls, not millions of micro-calls, so keep the span count believable
      // and the per-call cost in the tens-of-cents range.
      const m = rng() < 0.75 ? pool[0] : pool[1];
      const turns = 1 + Math.floor(rng() * 4);
      for (let t = 0; t < turns; t++) {
        // Realistic-ish per-call token counts (large RAG-style contexts).
        // research_agent runs bigger context windows; chatbot is lighter.
        const scale =
          feat.tag === "research_agent" ? 1.8 : feat.tag === "chatbot" ? 0.6 : 1.0;
        const inTok = Math.round((18_000 + rng() * 42_000) * scale);
        const outTok = Math.round((3_000 + rng() * 9_000) * scale);
        const tsNs = tsBaseNs + t * 2_000_000_000; // +2s per turn
        spans.push(
          chatSpan(tsNs, sessionId, uHash, m, inTok, outTok, feat.tag),
        );
        const c = chatCostUsd(m, inTok, outTok);
        featCostUsd += c;
        llmCostUsd += c;
      }

      // Conversion + feedback events keyed off this session's provider.
      const occurredNs = tsBaseNs + turns * 2_000_000_000;
      if (rng() < POSITIVE_FEEDBACK_RATE) {
        events.push(feedbackEvent(occurredNs, uHash));
        feedback++;
      }
      const convRate =
        provider === "openai" ? CONVERSION_RATE.openai : CONVERSION_RATE.anthropic;
      if (rng() < convRate) {
        const valueMicro = Math.round(
          CONVERSION_MIN_MICRO +
            rng() * (CONVERSION_MAX_MICRO - CONVERSION_MIN_MICRO),
        );
        events.push(conversionEvent(occurredNs, uHash, valueMicro));
        conversions++;
      }
    }
  }

  // --- Tool spans — a bounded count (~8% of the LLM span count) -------------
  const llmSpanCount = spans.length;
  const toolSpanCount = Math.round(llmSpanCount * TOOL_SPAN_SHARE);
  for (let n = 0; n < toolSpanCount; n++) {
    const feat = pickWeighted(rng, FEATURE_MIX);
    const provider = rng() < OPENAI_SHARE ? "openai" : "anthropic";
    const tool = TOOL_NAMES[Math.floor(rng() * TOOL_NAMES.length)];
    const sessionId = `backfill-${args.seed}-${hexId(4)}`;
    spans.push(
      toolSpan(randTsNs(), sessionId, nextUser(), provider, tool, feat),
    );
    toolCostUsd += TOOL_COST_MICRO_USD[tool] / 1_000_000;
  }

  // --- Embedding spans — a bounded count (~1.5% of the LLM span count) ------
  const embedSpanCount = Math.round(llmSpanCount * EMBED_SPAN_SHARE);
  for (let n = 0; n < embedSpanCount; n++) {
    const feat = pickWeighted(rng, FEATURE_MIX);
    const sessionId = `backfill-${args.seed}-${hexId(4)}`;
    // text-embedding-3-small @ $0.02/Mtok — large-ish RAG batches.
    const inTok = 20_000 + Math.floor(rng() * 80_000);
    spans.push(embeddingSpan(randTsNs(), sessionId, nextUser(), inTok, feat));
    embedCostUsd += (inTok * EMBED_USD_PER_MTOK) / 1_000_000;
  }

  return {
    spans,
    events,
    llmCostUsd,
    toolCostUsd,
    embedCostUsd,
    conversions,
    feedback,
  };
}

function pickWeighted(
  rng: () => number,
  mix: { tag: string; share: number }[],
): string {
  const r = rng();
  let acc = 0;
  for (const m of mix) {
    acc += m.share;
    if (r <= acc) return m.tag;
  }
  return mix[mix.length - 1].tag;
}

// ---------------------------------------------------------------------------
// POST batching
// ---------------------------------------------------------------------------

const SPANS_PER_BATCH = 500;
const EVENTS_PER_BATCH = 500;

async function postBatch(
  args: Args,
  n: number,
  spans: Record<string, unknown>[],
  events: Record<string, unknown>[],
): Promise<void> {
  const batch = {
    tenant_id: args.tenant,
    sdk_version: "vercel-chatbot-backfill/0.1",
    batch_id: batchId(args.seed, n),
    resource_spans: spans,
    business_events: events,
  };
  if (args.dryRun) return;
  const res = await fetch(args.gatewayUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(batch),
  });
  if (!res.ok) {
    throw new Error(
      `POST ${args.gatewayUrl} ${res.status}: ${await res.text()}`,
    );
  }
}

function fmtUsd(usd: number): string {
  return `$${usd.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  console.log(
    `Backfilling ~${args.days}d of synthetic chatbot traffic ` +
      `(target ${fmtUsd(args.targetUsd)}, seed=${args.seed}, tenant=${args.tenant})…`,
  );
  console.log(
    "  NOTE: synthetic-seed data — backdated spans, no LLM calls, $0 API spend.",
  );

  const gen = generate(args);
  const allInUsd = gen.llmCostUsd + gen.toolCostUsd + gen.embedCostUsd;
  console.log(
    `  · generated ${gen.spans.length} spans + ${gen.events.length} events ` +
      `(${gen.conversions} conversions, ${gen.feedback} positive_feedback)`,
  );
  console.log(
    `  · LLM ${fmtUsd(gen.llmCostUsd)} · Tools ${fmtUsd(gen.toolCostUsd)} ` +
      `· Embeddings ${fmtUsd(gen.embedCostUsd)} · all-in ${fmtUsd(allInUsd)}`,
  );

  // Chunk spans and events into batches with deterministic batch_ids.
  let batchN = 0;
  let posted = 0;
  for (let i = 0; i < gen.spans.length; i += SPANS_PER_BATCH) {
    const chunk = gen.spans.slice(i, i + SPANS_PER_BATCH);
    await postBatch(args, batchN++, chunk, []);
    posted += chunk.length;
    if (batchN % 10 === 0) {
      console.log(`  · posted ${posted}/${gen.spans.length} spans`);
    }
  }
  for (let i = 0; i < gen.events.length; i += EVENTS_PER_BATCH) {
    const chunk = gen.events.slice(i, i + EVENTS_PER_BATCH);
    await postBatch(args, batchN++, [], chunk);
  }

  console.log("");
  console.log(
    `✓ Backfill ${args.dryRun ? "(dry-run) " : ""}done. ` +
      `${gen.spans.length} spans + ${gen.events.length} events in ${batchN} batches. ` +
      `All-in seed cost ≈ ${fmtUsd(allInUsd)}. Re-run at --seed ${args.seed} is idempotent.`,
  );
}

main().catch((err) => {
  console.error("backfill-spans failed:", err);
  process.exit(1);
});
