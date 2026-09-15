// SPDX-License-Identifier: Apache-2.0
// CTO-371: the docs site renders docs/public-api/connect-snippets.json, so this test is the drift
// check. It fails when connectSnippets.ts changes and the committed export was not regenerated.
// Regenerate with: UPDATE_DOCS_ARTIFACTS=1 npx vitest run lib/connectSnippetsExport.test.ts
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  DOCS_PLACEHOLDER_KEY,
  HOSTED_INGEST_URL,
  connectSnippetsForDocs,
  renderConnectSnippetsExport,
} from "./connectSnippetsExport";

const COMMITTED = resolve(__dirname, "../../docs/public-api/connect-snippets.json");

describe("connect snippets docs export", () => {
  it("uses the placeholder key and the hosted endpoints, never a real key", () => {
    const out = connectSnippetsForDocs();
    expect(out.endpoints.openaiProxyBaseUrl).toBe(`${HOSTED_INGEST_URL}/openai/v1`);
    expect(out.endpoints.anthropicProxyBaseUrl).toBe(`${HOSTED_INGEST_URL}/anthropic`);
    expect(out.endpoints.proxyDeployed).toBe(true);
    const all = [...out.snippets.proxy, ...out.snippets.sdk];
    expect(out.snippets.proxy.map((s) => s.id)).toEqual(["proxy-openai", "proxy-anthropic"]);
    expect(out.snippets.sdk.map((s) => s.id)).toEqual(["sdk-python"]);
    for (const s of all) {
      expect(s.code).toContain(DOCS_PLACEHOLDER_KEY);
      expect(s.code).not.toMatch(/tally_sk_live_/);
    }
  });

  it("matches the committed docs/public-api/connect-snippets.json", () => {
    const text = renderConnectSnippetsExport();
    if (process.env.UPDATE_DOCS_ARTIFACTS === "1") {
      mkdirSync(dirname(COMMITTED), { recursive: true });
      writeFileSync(COMMITTED, text);
    }
    expect(existsSync(COMMITTED), `${COMMITTED} is missing; regenerate it`).toBe(true);
    expect(readFileSync(COMMITTED, "utf8"), "connect-snippets.json is stale; regenerate it").toBe(text);
  });
});
