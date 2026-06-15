// ai-tally: switched from Vercel AI Gateway to direct OpenAI/Anthropic SDK
// providers so the demo runs without a Vercel account. The chatbot template's
// model IDs ("moonshotai/kimi-k2.5", etc.) are gateway-only — map them to
// real provider model names here. Real provider is preserved on the span
// for the dashboard via the existing instrumentation patch.
import { anthropic } from "@ai-sdk/anthropic";
import { openai } from "@ai-sdk/openai";
import { customProvider } from "ai";
import { resolveLatest } from "../resolveModel";
import { isTestEnvironment } from "../constants";

export const myProvider = isTestEnvironment
  ? (() => {
      const { chatModel, titleModel } = require("./models.mock");
      return customProvider({
        languageModels: {
          "chat-model": chatModel,
          "title-model": titleModel,
        },
      });
    })()
  : null;

// Map the picker's `<provider>/<model>` IDs (see lib/ai/models.ts) to the
// matching SDK client. The picker's IDs are the SOURCE OF TRUTH for what the
// user can choose; this function just routes them. If the prefix is missing
// or unknown, fall back to TALLY_DEMO_PROVIDER (default anthropic, since
// OpenAI quota is unreliable in demo environments).
const DEFAULT_PROVIDER = process.env.TALLY_DEMO_PROVIDER ?? "anthropic";

// CTO-109: prefer the gateway's auto-discovered cache so a provider retiring
// a SKU doesn't break this route. The literal ids are last-resort fallbacks.
const FALLBACK_ANTHROPIC = resolveLatest("anthropic", "sonnet", "claude-sonnet-4-5");
const FALLBACK_OPENAI = resolveLatest("openai", "mini", "gpt-4o-mini");
const FALLBACK_ANTHROPIC_TITLE = resolveLatest("anthropic", "haiku", "claude-haiku-4-5");

function resolve(modelId: string) {
  // Honor an explicit anthropic/<model> id — strip the prefix; pass the rest
  // straight to @ai-sdk/anthropic so e.g. "claude-opus-4-8" routes correctly.
  if (modelId.startsWith("anthropic/")) {
    return anthropic(modelId.slice("anthropic/".length));
  }
  if (modelId.startsWith("openai/")) {
    return openai(modelId.slice("openai/".length));
  }
  // Legacy / unprefixed ids: keyword-match Claude family, else default.
  if (modelId.includes("claude")) {
    return anthropic(FALLBACK_ANTHROPIC);
  }
  return DEFAULT_PROVIDER === "openai"
    ? openai(FALLBACK_OPENAI)
    : anthropic(FALLBACK_ANTHROPIC);
}

export function getLanguageModel(modelId: string) {
  if (isTestEnvironment && myProvider) {
    return myProvider.languageModel(modelId);
  }
  return resolve(modelId);
}

export function getTitleModel() {
  if (isTestEnvironment && myProvider) {
    return myProvider.languageModel("title-model");
  }
  return DEFAULT_PROVIDER === "openai"
    ? openai(FALLBACK_OPENAI)
    : anthropic(FALLBACK_ANTHROPIC_TITLE);
}
