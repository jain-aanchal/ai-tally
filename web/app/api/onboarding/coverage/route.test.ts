// SPDX-License-Identifier: Apache-2.0
// Coverage proxy route (CTO-261, onboarding-agent §7). The gateway owns the probe; this handler is
// the service-token seam. What is tested here is what happens when that call does NOT go well,
// because the answer must be "we could not tell", never "your instrumentation is missing".
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET } from "./route";
import type { LayerCoverage } from "@/lib/firstEvent";

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function call(url = "http://test/api/onboarding/coverage"): Promise<LayerCoverage[]> {
  const res = await GET(new Request(url));
  return ((await res.json()) as { layers: LayerCoverage[] }).layers;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GET /api/onboarding/coverage", () => {
  it("passes the gateway's per-layer report through", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      reply({
        layers: [
          { layer: "llm", state: "covered", reason: "4 spans", proving_spans: 4 },
          { layer: "tools", state: "not_wired", reason: "no tool span", proving_spans: 0 },
          { layer: "vector", state: "not_wired", reason: "no vector span", proving_spans: 0 },
          {
            layer: "embeddings",
            state: "not_wired",
            reason: "no embeddings span",
            proving_spans: 0,
          },
          { layer: "account", state: "covered", reason: "2 rollup rows", proving_spans: 2 },
        ],
      }),
    );
    const layers = await call();
    expect(layers.map((l) => l.state)).toEqual([
      "covered",
      "not_wired",
      "not_wired",
      "not_wired",
      "covered",
    ]);
  });

  it("forwards the wired claim and softens a dark layer with it", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      reply({
        layers: [{ layer: "tools", state: "not_wired", reason: "no tool span", proving_spans: 0 }],
      }),
    );
    const layers = await call("http://test/api/onboarding/coverage?wired=tools");
    expect(String(fetchMock.mock.calls[0][0])).toContain("wired=tools");
    expect(layers.find((l) => l.layer === "tools")?.state).toBe("awaiting_first_event");
  });

  it("reports unknown, not not-wired, when the gateway errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(reply({ detail: "boom" }, 503));
    const layers = await call();
    expect(layers.every((l) => l.state === "unknown" && l.provingSpans === null)).toBe(true);
    expect(layers[0].reason).toMatch(/HTTP 503/);
  });

  it("reports unknown when the gateway cannot be reached at all", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("ECONNREFUSED"));
    const layers = await call();
    expect(layers.every((l) => l.state === "unknown")).toBe(true);
    expect(layers[0].reason).toMatch(/could not be reached/);
  });

  it("never lights a layer green on a covered claim with no proving span", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      reply({
        layers: [{ layer: "llm", state: "covered", reason: "trust me", proving_spans: 0 }],
      }),
    );
    expect((await call())[0].state).toBe("unknown");
  });
});
