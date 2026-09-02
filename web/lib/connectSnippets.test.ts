// SPDX-License-Identifier: Apache-2.0
// One-step-connect snippet generator (Initiative 2, §9).
import { describe, expect, it } from "vitest";

import { connectSnippets, defaultEndpoints } from "./connectSnippets";

const KEY = "tally_sk_live_TESTKEY123";

describe("connectSnippets", () => {
  it("inlines the real key into every path and provider", () => {
    const s = connectSnippets(KEY);
    const all = [...s.proxy, ...s.sdk];
    // Every snippet carries the key: the one-time creation view is the only place it appears.
    for (const snip of all) {
      expect(snip.code).toContain(KEY);
    }
    // Both proxy providers plus the SDK python one-liner.
    expect(s.proxy.map((x) => x.id)).toEqual(["proxy-openai", "proxy-anthropic"]);
    expect(s.sdk.map((x) => x.id)).toEqual(["sdk-python"]);
  });

  it("proxy snippets send the key as X-Tenant-Key, never as the provider credential", () => {
    const s = connectSnippets(KEY);
    const openai = s.proxy.find((x) => x.id === "proxy-openai")!;
    expect(openai.code).toContain(`X-Tenant-Key: ${KEY}`);
    // The provider key stays the provider's own env var, never the tally key.
    expect(openai.code).toContain("Authorization: Bearer $OPENAI_API_KEY");
    expect(openai.code).not.toContain(`Authorization: Bearer ${KEY}`);

    const anthropic = s.proxy.find((x) => x.id === "proxy-anthropic")!;
    expect(anthropic.code).toContain(`X-Tenant-Key: ${KEY}`);
    expect(anthropic.code).toContain("x-api-key: $ANTHROPIC_API_KEY");
    expect(anthropic.code).toContain("anthropic-version");
  });

  it("the SDK snippet is a tally.init one-liner with the key", () => {
    const python = connectSnippets(KEY).sdk[0];
    expect(python.code).toContain(`tally.init("${KEY}")`);
    expect(python.language).toBe("python");
  });

  it("threads a custom SDK endpoint into init() when configured", () => {
    const endpoints = { ...defaultEndpoints(), sdkEndpoint: "https://ingest.example.com" };
    const python = connectSnippets(KEY, endpoints).sdk[0];
    expect(python.code).toContain(`tally.init("${KEY}", endpoint="https://ingest.example.com")`);
  });

  it("proxy snippets carry the refresh-window note; the SDK one does not", () => {
    const s = connectSnippets(KEY);
    expect(s.proxy.every((x) => x.note && x.note.includes("few seconds"))).toBe(true);
    expect(s.sdk[0].note).not.toContain("few seconds");
  });

  it("defaults to the hosted proxy hostnames", () => {
    const e = defaultEndpoints();
    expect(e.openaiProxyBaseUrl).toContain("openai.proxy.ai-tally.com");
    expect(e.anthropicProxyBaseUrl).toContain("anthropic.proxy.ai-tally.com");
  });
});
