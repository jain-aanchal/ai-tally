// ai-tally: added file. Helper module — POSTs spans + CDP events to the local
// ai-tally gateway and classifies prompts into feature tags. CTO-106 retired
// the gpt-5-mini pinning workaround that previously sat here: outbound spans
// now carry real provider/model on the standard gen_ai.* attributes and the
// gateway's enrich_cost computes authoritative cost from the catalog.
// Marked with `ai-tally:` so a future Vercel template refresh can replay
// every patch cleanly.

import crypto from "node:crypto";

const GATEWAY_URL =
  process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080/v1/batches";
const TENANT = process.env.TALLY_TENANT ?? "local-dev";
const FEATURE_TAG_DEFAULT = "chatbot-demo";

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

export interface SpanInput {
  sessionId: string;
  userHash?: string;
  realProvider: "openai" | "anthropic";
  realModel: string;
  promptText: string;
  inputTokens: number;
  outputTokens: number;
  /** End-to-end completion duration in ms (drives the p95 latency column on /compare). */
  durationMs?: number;
  featureTagOverride?: FeatureTag;
  runId?: string;
}

export async function postSpan(input: SpanInput): Promise<void> {
  const featureTag =
    input.featureTagOverride ?? classifyFeatureTag(input.promptText);
  const userHash = input.userHash ?? sessionUserHash(input.sessionId);

  const span: Record<string, unknown> = {
    // structural
    ServiceName: "vercel-chatbot",
    SpanName: "chat.completion",
    trace_id: hexId(16),
    span_id: hexId(8),
    timestamp_ns: Date.now() * 1_000_000,
    duration_ns: Math.max(0, Math.round((input.durationMs ?? 0) * 1_000_000)),
    status_code: 1,
    // gen_ai.* — real provider/model since CTO-106 expanded the seed catalog
    // to cover them. The gateway's enrich_cost computes authoritative cost
    // from input_tokens + output_tokens; we don't send an estimated cost hint.
    "gen_ai.system": input.realProvider,
    "gen_ai.request.model": input.realModel,
    "gen_ai.response.model": input.realModel,
    "gen_ai.operation.name": "chat",
    "gen_ai.usage.input_tokens": input.inputTokens,
    "gen_ai.usage.output_tokens": input.outputTokens,
    "gen_ai.feature_tag": featureTag,
    "gen_ai.session_id": input.sessionId,
    "gen_ai.user_id_hash": userHash,
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
