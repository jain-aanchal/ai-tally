// SPDX-License-Identifier: Apache-2.0
// One-step-connect snippet generator (Initiative 2, §9).
import { describe, expect, it } from "vitest";

import { type ConnectEndpoints, connectSnippets, defaultEndpoints } from "./connectSnippets";

const KEY = "tally_sk_live_TESTKEY123";

/** A deployment that runs the hosted proxy, on a domain that is deliberately not ai-tally's. */
const DEPLOYED: ConnectEndpoints = {
  openaiProxyBaseUrl: "https://ingest.example.com/openai/v1",
  anthropicProxyBaseUrl: "https://ingest.example.com/anthropic",
  sdkEndpoint: "",
  proxyDeployed: true,
};

describe("connectSnippets", () => {
  it("inlines the real key into every path and provider", () => {
    const s = connectSnippets(KEY, DEPLOYED);
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
    const s = connectSnippets(KEY, DEPLOYED);
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
    const python = connectSnippets(KEY, DEPLOYED).sdk[0];
    expect(python.code).toContain(`tally.init("${KEY}")`);
    expect(python.language).toBe("python");
  });

  it("threads a custom SDK endpoint into init() when configured", () => {
    const python = connectSnippets(KEY, { ...DEPLOYED, sdkEndpoint: "https://ingest.example.com" }).sdk[0];
    expect(python.code).toContain(`tally.init("${KEY}", endpoint="https://ingest.example.com")`);
  });

  it("proxy snippets carry the refresh-window note; the SDK one does not", () => {
    const s = connectSnippets(KEY, DEPLOYED);
    expect(s.proxy.every((x) => x.note && x.note.includes("few seconds"))).toBe(true);
    expect(s.sdk[0].note).not.toContain("few seconds");
  });

  it("offers no proxy snippets when the deployment has no hosted proxy", () => {
    // A snippet for a proxy that does not exist would send the customer's traffic, provider key
    // included, to a hostname this deployment does not serve.
    const s = connectSnippets(KEY, { ...DEPLOYED, proxyDeployed: false });
    expect(s.proxy).toEqual([]);
    expect(s.sdk).toHaveLength(1);
  });
});

describe("defaultEndpoints", () => {
  it("derives both proxy URLs from this deployment's TALLY_INGEST_URL", () => {
    const e = defaultEndpoints({ TALLY_INGEST_URL: "https://ingest.example.com/" });
    expect(e.openaiProxyBaseUrl).toBe("https://ingest.example.com/openai/v1");
    expect(e.anthropicProxyBaseUrl).toBe("https://ingest.example.com/anthropic");
    expect(e.proxyDeployed).toBe(true);
  });

  it("has no hardcoded hostname: a deployment that configured nothing has no proxy", () => {
    // The old fallback was ingest.ai-tally.com, which pointed every other deployment's users at
    // ai-tally's proxy.
    const e = defaultEndpoints({});
    expect(e.proxyDeployed).toBe(false);
    expect(e.openaiProxyBaseUrl).toBe("");
    expect(JSON.stringify(e)).not.toContain("ai-tally.com");
  });

  it("an explicit NEXT_PUBLIC override wins over the derived URL", () => {
    const e = defaultEndpoints({
      TALLY_INGEST_URL: "https://ingest.example.com",
      NEXT_PUBLIC_TALLY_OPENAI_PROXY_URL: "https://openai.gateway.example.net/v1",
    });
    expect(e.openaiProxyBaseUrl).toBe("https://openai.gateway.example.net/v1");
    expect(e.anthropicProxyBaseUrl).toBe("https://ingest.example.com/anthropic");
  });

  it("treats a blank TALLY_INGEST_URL (deploy.sh with the proxy off) as no proxy", () => {
    expect(defaultEndpoints({ TALLY_INGEST_URL: "  " }).proxyDeployed).toBe(false);
  });
});
