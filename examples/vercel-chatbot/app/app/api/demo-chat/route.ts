// ai-tally: added file. A self-contained, no-auth, no-DB chat route used by
// the `make chatbot-demo` traffic driver. The upstream `(chat)/api/chat/route.ts`
// is wired to NextAuth + Postgres + Vercel Blob which the demo deliberately
// doesn't depend on — so the driver hits this minimal endpoint instead.
// Every `gen_ai.*` and `chatbot.*` attribute is identical to what the patched
// upstream route would emit, so the workflow-2/3/4 dashboards see the same
// shape regardless of which path produced it.

import { type NextRequest, NextResponse } from "next/server";
import { resolveLatest } from "@/lib/resolveModel";
import {
  classifyFeatureTag,
  type FeatureTag,
  postCdpEvent,
  postSpan,
  postToolSpan,
  sessionUserHash,
} from "@/lib/tally";

// ai-tally (CTO-137): the synthetic driver hits THIS route (not the upstream
// (chat) UI route), so to fill the Cost tab's Tools bar on a live
// `make chatbot-demo` we detect tool-shaped prompts here and emit a matching
// tool span. Same fixed price table as the UI route. Demo-seed pricing — not a
// real billing model.
const TOOL_COST_MICRO_USD: Record<string, number> = {
  getWeather: 1_000,
  createDocument: 5_000,
  updateDocument: 5_000,
  editDocument: 5_000,
  requestSuggestions: 2_000,
};

// Map a prompt to the demo tool it would have triggered, mirroring the upstream
// route's tool set. Keyword-based and intentionally coarse — this is scripted
// demo traffic, not a real tool router.
function inferDemoTool(prompt: string): string | undefined {
  const t = prompt.toLowerCase();
  if (
    t.includes("weather") ||
    t.includes("forecast") ||
    t.includes("temperature")
  ) {
    return "getWeather";
  }
  if (
    t.includes("draft a doc") ||
    t.includes("write a document") ||
    t.includes("create a doc")
  ) {
    return "createDocument";
  }
  if (t.includes("suggestion") && t.includes("document")) {
    return "requestSuggestions";
  }
  return undefined;
}

// ai-tally: Next.js 16 cacheComponents rejects route segment config
// (`runtime`, `dynamic`). POST routes are dynamic by default; Node runtime
// is the default for this template.

interface DemoChatBody {
  sessionId: string;
  prompt: string;
  provider: "openai" | "anthropic";
  model?: string;
  turnIndex?: number;
  featureTag?: FeatureTag;
  // When the driver sets this, the route skips the live provider call and uses
  // a deterministic fake response. Makes CI runs and "no API key" demos work.
  dryRun?: boolean;
}

// CTO-109: resolve the current cheapest model in each family from the gateway's
// auto-discovered cache (.tally/models.json) instead of hardcoding SKUs. An
// explicit env override still wins; the literal id is the last-resort fallback
// the resolver returns only when the cache is missing or has no in-family match.
const DEFAULT_OPENAI_MODEL =
  process.env.TALLY_OPENAI_MODEL ?? resolveLatest("openai", "mini", "gpt-4o-mini");
const DEFAULT_ANTHROPIC_MODEL =
  process.env.TALLY_ANTHROPIC_MODEL ??
  resolveLatest("anthropic", "haiku", "claude-haiku-4-5");

interface ProviderResult {
  text: string;
  inputTokens: number;
  outputTokens: number;
}

async function callOpenAI(prompt: string, model: string): Promise<ProviderResult> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("OPENAI_API_KEY not set");
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: "user", content: prompt }],
      max_tokens: 256,
    }),
  });
  if (!res.ok) throw new Error(`openai ${res.status}: ${await res.text()}`);
  const json = (await res.json()) as {
    choices: { message: { content: string } }[];
    usage: { prompt_tokens: number; completion_tokens: number };
  };
  return {
    text: json.choices[0]?.message?.content ?? "",
    inputTokens: json.usage.prompt_tokens,
    outputTokens: json.usage.completion_tokens,
  };
}

async function callAnthropic(
  prompt: string,
  model: string,
): Promise<ProviderResult> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error("ANTHROPIC_API_KEY not set");
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model,
      max_tokens: 256,
      messages: [{ role: "user", content: prompt }],
    }),
  });
  if (!res.ok) throw new Error(`anthropic ${res.status}: ${await res.text()}`);
  const json = (await res.json()) as {
    content: { text: string }[];
    usage: { input_tokens: number; output_tokens: number };
  };
  return {
    text: json.content[0]?.text ?? "",
    inputTokens: json.usage.input_tokens,
    outputTokens: json.usage.output_tokens,
  };
}

function fakeResult(prompt: string): ProviderResult {
  // Deterministic token estimate so dry-runs still produce realistic-looking
  // ClickHouse rows (>$0 EstimatedCost, distinguishable by feature tag).
  const inputTokens = Math.max(20, Math.round(prompt.length / 4));
  const outputTokens = Math.max(40, Math.round(prompt.length / 2));
  return {
    text: `(demo dry-run response to: ${prompt.slice(0, 64)}…)`,
    inputTokens,
    outputTokens,
  };
}

export async function POST(req: NextRequest) {
  let body: DemoChatBody;
  try {
    body = (await req.json()) as DemoChatBody;
  } catch {
    return NextResponse.json({ error: "bad json" }, { status: 400 });
  }
  if (!body.sessionId || !body.prompt || !body.provider) {
    return NextResponse.json(
      { error: "sessionId, prompt, provider required" },
      { status: 422 },
    );
  }

  const model =
    body.model ??
    (body.provider === "openai" ? DEFAULT_OPENAI_MODEL : DEFAULT_ANTHROPIC_MODEL);
  const featureTag = body.featureTag ?? classifyFeatureTag(body.prompt);
  const userHash = sessionUserHash(body.sessionId);

  let result: ProviderResult;
  try {
    if (body.dryRun) {
      result = fakeResult(body.prompt);
    } else if (body.provider === "openai") {
      result = await callOpenAI(body.prompt, model);
    } else {
      result = await callAnthropic(body.prompt, model);
    }
  } catch (err) {
    return NextResponse.json(
      { error: (err as Error).message },
      { status: 502 },
    );
  }

  // ai-tally: emit the cost span on the happy path. Fire-and-forget — never
  // block the response on the gateway being slow.
  void postSpan({
    sessionId: body.sessionId,
    userHash,
    realProvider: body.provider,
    realModel: model,
    promptText: body.prompt,
    inputTokens: result.inputTokens,
    outputTokens: result.outputTokens,
    featureTagOverride: featureTag,
    runId: body.sessionId,
  });

  // ai-tally (CTO-137): if the prompt is tool-shaped, emit a tool span so the
  // Cost tab's Tools bar is non-zero on a live demo run. Fire-and-forget.
  const demoTool = inferDemoTool(body.prompt);
  if (demoTool) {
    void postToolSpan({
      sessionId: body.sessionId,
      userHash,
      provider: body.provider,
      tool: demoTool,
      costMicroUsd: TOOL_COST_MICRO_USD[demoTool] ?? 1_000,
      runId: body.sessionId,
      featureTagOverride: featureTag,
    });
  }

  // ai-tally: after the 5th message in a session, signal engagement so the
  // attribution dashboard can show $/engaged-session alongside $/conversion.
  if ((body.turnIndex ?? 0) === 5) {
    void postCdpEvent({
      sessionId: body.sessionId,
      userHash,
      type: "session_engaged",
      featureTag,
    });
  }

  return NextResponse.json({
    reply: result.text,
    inputTokens: result.inputTokens,
    outputTokens: result.outputTokens,
    featureTag,
    provider: body.provider,
    model,
  });
}
