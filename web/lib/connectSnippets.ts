// SPDX-License-Identifier: Apache-2.0
// One-step-connect snippet generator (Initiative 2, §9). Shown right after a key is minted, with the
// real token inlined into that ONE-TIME creation view. No secret is stored to render it later: the
// key is passed in from the mint response and lives only in the client component that shows it once.
//
// Two paths, both keyed by the same per-org ingest key (Initiative 2, Decision 2):
//   * Proxy (zero-code): point the provider base URL at the hosted ai-tally proxy and send the key
//     as the X-Tenant-Key header. Any language; shown here as a shell/curl block per provider.
//   * SDK (deep context): `pip install tally` then `tally.init(key)` auto-instruments the official
//     openai / anthropic clients.
//
// Pure and framework-free so it is unit-tested directly; the React view maps over the result.

/** The endpoints the snippets point at, resolved for THIS deployment (see defaultEndpoints). */
export interface ConnectEndpoints {
  openaiProxyBaseUrl: string;
  anthropicProxyBaseUrl: string;
  /** The SDK ingest endpoint. Empty string means "use the SDK default" and is omitted from init(). */
  sdkEndpoint: string;
  /**
   * Whether this deployment runs a hosted proxy at all. When false there are no proxy snippets: a
   * snippet for a proxy that does not exist would send the customer's traffic, and their provider
   * key with it, to a hostname this deployment does not serve.
   */
  proxyDeployed: boolean;
}

/**
 * Resolve the connect endpoints for this deployment. Server-side: pass the result to the client.
 *
 * Order, per proxy URL:
 *   1. An explicit NEXT_PUBLIC_TALLY_*_PROXY_URL override.
 *   2. Derived from TALLY_INGEST_URL (the hosted proxy's base URL, set by deploy/demo when
 *      INGEST_DOMAIN turns the proxy on), plus the path-mode provider prefix the proxy strips.
 *   3. Nothing: this deployment has no hosted proxy, so proxyDeployed is false.
 *
 * There is deliberately no hardcoded hostname. The previous fallback, ingest.ai-tally.com (and before
 * it openai.proxy.ai-tally.com), was right for exactly one deployment and wrong for every other:
 * anyone running this kit on their own domain showed their users snippets aimed at ai-tally's proxy.
 *
 * TALLY_INGEST_URL is not NEXT_PUBLIC_, so it is read on the server at request time. That is why the
 * keys page resolves this and passes it down, rather than the client component calling it: in the
 * browser only the NEXT_PUBLIC_ overrides would be visible.
 */
export function defaultEndpoints(env: Record<string, string | undefined> = process.env): ConnectEndpoints {
  const base = (env.TALLY_INGEST_URL ?? "").trim().replace(/\/+$/, "");
  const openaiProxyBaseUrl = env.NEXT_PUBLIC_TALLY_OPENAI_PROXY_URL || (base ? `${base}/openai/v1` : "");
  const anthropicProxyBaseUrl =
    env.NEXT_PUBLIC_TALLY_ANTHROPIC_PROXY_URL || (base ? `${base}/anthropic` : "");
  return {
    openaiProxyBaseUrl,
    anthropicProxyBaseUrl,
    sdkEndpoint: env.NEXT_PUBLIC_TALLY_INGEST_URL ?? "",
    proxyDeployed: Boolean(openaiProxyBaseUrl && anthropicProxyBaseUrl),
  };
}

export type ConnectPath = "proxy" | "sdk";

export interface Snippet {
  /** Stable id for the tab/key. */
  id: string;
  /** Human label for the tab, e.g. "OpenAI" or "Python". */
  label: string;
  /** Syntax hint for the code block, e.g. "bash" or "python". */
  language: string;
  /** The copy-paste body, with the real key already inlined. */
  code: string;
  /** An honest caveat rendered under the block, or null. */
  note: string | null;
}

// The proxy path attributes a brand-new key only after the edge cache picks it up, up to one refresh
// interval (Initiative 2, §6.2/§9). Stated so a first event that lags a few seconds reads as expected,
// not broken. The SDK path has no such delay: the gateway resolves the key directly per request.
const PROXY_REFRESH_NOTE =
  "A brand-new key can take a few seconds to attribute while the proxy picks it up. Your provider key is unchanged and never sent to ai-tally.";

function proxyOpenAi(key: string, e: ConnectEndpoints): Snippet {
  return {
    id: "proxy-openai",
    label: "OpenAI",
    language: "bash",
    code: [
      "# Point the OpenAI SDK at the ai-tally proxy",
      `export OPENAI_BASE_URL="${e.openaiProxyBaseUrl}"`,
      "",
      "# ai-tally identifies your org by this ingest key, sent as the X-Tenant-Key header:",
      `#   X-Tenant-Key: ${key}`,
      "# e.g. a raw request:",
      `curl "${e.openaiProxyBaseUrl}/chat/completions" \\`,
      '  -H "Authorization: Bearer $OPENAI_API_KEY" \\',
      `  -H "X-Tenant-Key: ${key}" \\`,
      '  -H "Content-Type: application/json" \\',
      `  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'`,
    ].join("\n"),
    note: PROXY_REFRESH_NOTE,
  };
}

function proxyAnthropic(key: string, e: ConnectEndpoints): Snippet {
  return {
    id: "proxy-anthropic",
    label: "Anthropic",
    language: "bash",
    code: [
      "# Point the Anthropic SDK at the ai-tally proxy",
      `export ANTHROPIC_BASE_URL="${e.anthropicProxyBaseUrl}"`,
      "",
      "# Send your ai-tally ingest key as the X-Tenant-Key header:",
      `#   X-Tenant-Key: ${key}`,
      "# e.g. a raw request (your x-api-key and anthropic-version pass through untouched):",
      `curl "${e.anthropicProxyBaseUrl}/v1/messages" \\`,
      '  -H "x-api-key: $ANTHROPIC_API_KEY" \\',
      '  -H "anthropic-version: 2023-06-01" \\',
      `  -H "X-Tenant-Key: ${key}" \\`,
      '  -H "Content-Type: application/json" \\',
      `  -d '{"model":"claude-3-5-haiku-latest","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'`,
    ].join("\n"),
    note: PROXY_REFRESH_NOTE,
  };
}

function sdkPython(key: string, e: ConnectEndpoints): Snippet {
  const initArgs = e.sdkEndpoint
    ? `"${key}", endpoint="${e.sdkEndpoint}"`
    : `"${key}"`;
  return {
    id: "sdk-python",
    label: "Python",
    language: "python",
    code: [
      "# pip install tally",
      "import tally",
      "",
      `tally.init(${initArgs})`,
      "# From here your unmodified openai / anthropic calls are auto-instrumented:",
      "# model, tokens, and cost land in ai-tally with no per-call record_* code.",
    ].join("\n"),
    note: "Set with_account(...) once per request to unlock cost per customer; start_trace(feature_tag=...) for cost per feature.",
  };
}

/**
 * Build the connect snippets for a freshly minted key. The key is inlined into every snippet, so this
 * is only ever called from the one-time key-creation view (spec §9).
 */
export function connectSnippets(
  key: string,
  endpoints: ConnectEndpoints = defaultEndpoints(),
): Record<ConnectPath, Snippet[]> {
  return {
    proxy: endpoints.proxyDeployed ? [proxyOpenAi(key, endpoints), proxyAnthropic(key, endpoints)] : [],
    sdk: [sdkPython(key, endpoints)],
  };
}
