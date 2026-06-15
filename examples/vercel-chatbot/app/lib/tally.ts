// ai-tally: added file. Helper module — POSTs spans + CDP events to the local
// ai-tally gateway, classifies prompts into feature tags, and pins outbound
// spans to gpt-5-mini so the gateway's seed price catalog can compute cost
// authoritatively (real provider/model travel on long-tail attributes).
// Marked with `ai-tally:` so a future Vercel template refresh can replay
// every patch cleanly.

import crypto from "node:crypto";

const GATEWAY_URL =
  process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080/v1/batches";
const TENANT = process.env.TALLY_TENANT ?? "local-dev";
const FEATURE_TAG_DEFAULT = "chatbot-demo";

// Mirrors gateway/seed price catalog rates for gpt-5-mini (USD per 1M tokens).
// We pin spans to this model and back-compute tokens from the real provider's
// cost so EstimatedCost lands non-zero in ClickHouse. Tracked by CTO-106.
const PIN_MODEL = "gpt-5-mini";
const PIN_INPUT_PER_MTOK = 0.25;
const PIN_OUTPUT_PER_MTOK = 2.0;

export type FeatureTag =
  | "chatbot.support"
  | "chatbot.brainstorm"
  | "chatbot.code"
  | "chatbot-demo";

const SUPPORT_KEYWORDS = [
  "broken",
  "error",
  "doesn't work",
  "doesnt work",
  "won't",
  "wont",
  "help me",
  "stuck",
  "refund",
  "cancel",
  "subscription",
  "billing",
  "issue",
  "problem",
  "not working",
];

const CODE_KEYWORDS = [
  "function",
  "javascript",
  "typescript",
  "python",
  "rust",
  "code",
  "stack trace",
  "exception",
  "regex",
  "sql query",
  "compile",
  "import",
  "class ",
  "def ",
  "const ",
  "let ",
  "var ",
  "}",
  ";",
];

const BRAINSTORM_KEYWORDS = [
  "brainstorm",
  "ideas",
  "what should",
  "suggest",
  "explore",
  "options for",
  "what if",
  "could we",
  "should i",
  "imagine",
  "what are some",
];

export function classifyFeatureTag(text: string): FeatureTag {
  const lower = text.toLowerCase();
  let support = 0;
  let code = 0;
  let brainstorm = 0;
  for (const k of SUPPORT_KEYWORDS) if (lower.includes(k)) support++;
  for (const k of CODE_KEYWORDS) if (lower.includes(k)) code++;
  for (const k of BRAINSTORM_KEYWORDS) if (lower.includes(k)) brainstorm++;
  if (support === 0 && code === 0 && brainstorm === 0) return "chatbot-demo";
  if (support >= code && support >= brainstorm) return "chatbot.support";
  if (code >= brainstorm) return "chatbot.code";
  return "chatbot.brainstorm";
}

function uuidish(): string {
  return crypto.randomUUID();
}

function hexId(bytes: number): string {
  return crypto.randomBytes(bytes).toString("hex");
}

// ai-tally: stable per-session pseudo-hash. Real demo, fake users — we only
// need a 64-char hex string the gateway's PII check accepts.
export function sessionUserHash(sessionId: string): string {
  return crypto.createHash("sha256").update(`tally-demo:${sessionId}`).digest("hex");
}

function pinnedCostMicroUsd(inputTokens: number, outputTokens: number): number {
  const usd =
    (inputTokens * PIN_INPUT_PER_MTOK) / 1_000_000 +
    (outputTokens * PIN_OUTPUT_PER_MTOK) / 1_000_000;
  return Math.round(usd * 1_000_000);
}

export interface SpanInput {
  sessionId: string;
  userHash?: string;
  realProvider: "openai" | "anthropic";
  realModel: string;
  promptText: string;
  inputTokens: number;
  outputTokens: number;
  featureTagOverride?: FeatureTag;
  runId?: string;
}

export async function postSpan(input: SpanInput): Promise<void> {
  const featureTag =
    input.featureTagOverride ?? classifyFeatureTag(input.promptText);
  const userHash = input.userHash ?? sessionUserHash(input.sessionId);
  const costMicro = pinnedCostMicroUsd(input.inputTokens, input.outputTokens);

  const span: Record<string, unknown> = {
    // structural
    ServiceName: "vercel-chatbot",
    SpanName: "chat.completion",
    trace_id: hexId(16),
    span_id: hexId(8),
    timestamp_ns: Date.now() * 1_000_000,
    duration_ns: 0,
    status_code: 1,
    // gen_ai.* — pinned to gpt-5-mini so the seed catalog computes a non-zero
    // EstimatedCost. CTO-106 removes this workaround.
    "gen_ai.system": "openai",
    "gen_ai.request.model": PIN_MODEL,
    "gen_ai.response.model": PIN_MODEL,
    "gen_ai.operation.name": "chat",
    "gen_ai.usage.input_tokens": input.inputTokens,
    "gen_ai.usage.output_tokens": input.outputTokens,
    "gen_ai.cost.estimated_micro_usd": costMicro,
    "gen_ai.feature_tag": featureTag,
    "gen_ai.session_id": input.sessionId,
    "gen_ai.user_id_hash": userHash,
    // long-tail attributes — promoted into the SpanAttributes map by the
    // gateway. Workflow 2 / 4 read these to show the real provider.
    "chatbot.real_provider": input.realProvider,
    "chatbot.real_model": input.realModel,
    "chatbot.run_id": input.runId ?? "ad-hoc",
  };

  const batch = {
    tenant_id: TENANT,
    sdk_version: "vercel-chatbot/0.1",
    batch_id: uuidish(),
    resource_spans: [span],
  };

  try {
    await fetch(GATEWAY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(batch),
    });
  } catch (err) {
    // The demo is best-effort — never fail the chat completion because the
    // gateway hiccuped. Print so the operator notices in `run.sh` output.
    console.warn("[tally] postSpan failed:", (err as Error).message);
  }
}

export type CdpEventType =
  | "positive_feedback"
  | "session_engaged"
  | "conversion";

export interface EventInput {
  sessionId: string;
  userHash?: string;
  type: CdpEventType;
  valueMicroUsd?: number;
  featureTag?: FeatureTag | string;
}

export async function postCdpEvent(input: EventInput): Promise<void> {
  const userHash = input.userHash ?? sessionUserHash(input.sessionId);
  const nowNs = Date.now() * 1_000_000;
  const event = {
    business_event_id: uuidish(),
    event_name: input.type,
    user_id_hash: userHash,
    occurred_at_ns: nowNs,
    value_amount_micro: input.valueMicroUsd ?? null,
    value_currency: "USD",
    value_type: input.type === "conversion" ? "monetary" : "count",
    source: "vercel-chatbot",
  };
  const batch = {
    tenant_id: TENANT,
    sdk_version: "vercel-chatbot/0.1",
    batch_id: uuidish(),
    resource_spans: [],
    business_events: [event],
  };

  try {
    await fetch(GATEWAY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(batch),
    });
  } catch (err) {
    console.warn("[tally] postCdpEvent failed:", (err as Error).message);
  }
}
