// ai-tally: model list reflects what the demo can actually call. Once the
// runtime stopped using the Vercel AI Gateway, the template's stock IDs
// (deepseek/, moonshotai/, openai/gpt-oss-*, xai/) all became unreachable —
// the picker would happily select them and every send would 404. Listing the
// real OpenAI + Anthropic models we route through @ai-sdk/* keeps the UI
// honest.
//
// CTO-147: the OpenAI entries used to pin `gpt-4o` / `gpt-4o-mini`, which the
// provider has since retired — `make chatbot-demo` 404'd on every OpenAI send.
// They're now `gpt-5` / `gpt-5-mini` (both priced in tally.pricing.seed_catalog,
// so dashboard cost stays non-zero). run.sh refreshes .tally/models.json on each
// launch (TALLY_MODELS_REFRESH=1) so operators see the live lineup logged and
// providers.ts's resolveLatest() fallbacks track the current cheapest in-family
// id. This picker list is still hardcoded (the source of truth for the UI);
// wiring it to read the cache dynamically without rearchitecting the Vercel
// template's model picker is the CTO-147 follow-up. Every id below MUST exist in
// seed_catalog() or its cost silently drops to $0.
//
// CTO-109 owns the gateway/SDK discovery that writes the cache.
export const DEFAULT_CHAT_MODEL = "anthropic/claude-sonnet-4-5";

export const titleModel = {
  id: "anthropic/claude-haiku-4-5",
  name: "Claude Haiku 4.5",
  provider: "anthropic",
  description: "Fast model for title generation",
};

export type ModelCapabilities = {
  tools: boolean;
  vision: boolean;
  reasoning: boolean;
};

export type ChatModel = {
  id: string;
  name: string;
  provider: string;
  description: string;
  gatewayOrder?: string[];
  reasoningEffort?: "none" | "minimal" | "low" | "medium" | "high";
};

// ai-tally: every entry must resolve through lib/ai/providers.ts. Anthropic
// IDs are prefixed `anthropic/` so resolve() routes them to @ai-sdk/anthropic;
// OpenAI IDs route to @ai-sdk/openai. Adding a model here without a matching
// resolver branch produces a runtime 404.
export const chatModels: ChatModel[] = [
  {
    id: "anthropic/claude-sonnet-4-5",
    name: "Claude Sonnet 4.5",
    provider: "anthropic",
    description: "Anthropic flagship — best quality, mid latency",
  },
  {
    id: "anthropic/claude-haiku-4-5",
    name: "Claude Haiku 4.5",
    provider: "anthropic",
    description: "Fast, cheap, smart enough for most chat turns",
  },
  {
    id: "anthropic/claude-opus-4-8",
    name: "Claude Opus 4.8",
    provider: "anthropic",
    description: "Anthropic's most capable model — slower, pricier",
  },
  {
    id: "openai/gpt-5-mini",
    name: "GPT-5 mini",
    provider: "openai",
    description: "OpenAI cheap-and-fast — needs OPENAI_API_KEY with credit",
  },
  {
    id: "openai/gpt-5",
    name: "GPT-5",
    provider: "openai",
    description: "OpenAI flagship — needs OPENAI_API_KEY with credit",
  },
  {
    id: "google/gemini-3-flash",
    name: "Gemini 3 Flash",
    provider: "google",
    description:
      "Google fast-and-cheap — needs GOOGLE_API_KEY or GEMINI_API_KEY",
  },
  {
    id: "google/gemini-2.5-pro",
    name: "Gemini 2.5 Pro",
    provider: "google",
    description:
      "Google flagship — higher quality, pricier; needs GOOGLE_API_KEY or GEMINI_API_KEY",
  },
];

export async function getCapabilities(): Promise<
  Record<string, ModelCapabilities>
> {
  const results = await Promise.all(
    chatModels.map(async (model) => {
      try {
        const res = await fetch(
          `https://ai-gateway.vercel.sh/v1/models/${model.id}/endpoints`,
          { next: { revalidate: 86_400 } }
        );
        if (!res.ok) {
          return [model.id, { tools: false, vision: false, reasoning: false }];
        }

        const json = await res.json();
        const endpoints = json.data?.endpoints ?? [];
        const params = new Set(
          endpoints.flatMap(
            (e: { supported_parameters?: string[] }) =>
              e.supported_parameters ?? []
          )
        );
        const inputModalities = new Set(
          json.data?.architecture?.input_modalities ?? []
        );

        return [
          model.id,
          {
            tools: params.has("tools"),
            vision: inputModalities.has("image"),
            reasoning: params.has("reasoning"),
          },
        ];
      } catch {
        return [model.id, { tools: false, vision: false, reasoning: false }];
      }
    })
  );

  return Object.fromEntries(results);
}

export const isDemo = process.env.IS_DEMO === "1";

type GatewayModel = {
  id: string;
  name: string;
  type?: string;
  tags?: string[];
};

export type GatewayModelWithCapabilities = ChatModel & {
  capabilities: ModelCapabilities;
};

export async function getAllGatewayModels(): Promise<
  GatewayModelWithCapabilities[]
> {
  try {
    const res = await fetch("https://ai-gateway.vercel.sh/v1/models", {
      next: { revalidate: 86_400 },
    });
    if (!res.ok) {
      return [];
    }

    const json = await res.json();
    return (json.data ?? [])
      .filter((m: GatewayModel) => m.type === "language")
      .map((m: GatewayModel) => ({
        id: m.id,
        name: m.name,
        provider: m.id.split("/")[0],
        description: "",
        capabilities: {
          tools: m.tags?.includes("tool-use") ?? false,
          vision: m.tags?.includes("vision") ?? false,
          reasoning: m.tags?.includes("reasoning") ?? false,
        },
      }));
  } catch {
    return [];
  }
}

export function getActiveModels(): ChatModel[] {
  return chatModels;
}

export const allowedModelIds = new Set(chatModels.map((m) => m.id));

export const modelsByProvider = chatModels.reduce(
  (acc, model) => {
    if (!acc[model.provider]) {
      acc[model.provider] = [];
    }
    acc[model.provider].push(model);
    return acc;
  },
  {} as Record<string, ChatModel[]>
);
