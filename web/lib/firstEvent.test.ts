// SPDX-License-Identifier: Apache-2.0
// First-data onboarding status mapper (Initiative 2, §9).
import { describe, expect, it } from "vitest";

import {
  COVERAGE_LAYERS,
  firstEventStatus,
  layerCoverageState,
  parseCoverage,
  unknownCoverage,
} from "./firstEvent";

describe("firstEventStatus", () => {
  it("maps a found row to connected", () => {
    expect(firstEventStatus(true)).toBe("connected");
  });

  it("maps a ran-but-empty probe to waiting", () => {
    expect(firstEventStatus(false)).toBe("waiting");
  });

  it("maps an unrunnable probe to unknown, never a fabricated waiting", () => {
    // null = ClickHouse unreachable. Honest under uncertainty: not collapsed into a definite "no".
    expect(firstEventStatus(null)).toBe("unknown");
  });
});

describe("per-layer coverage (CTO-261)", () => {
  it("derives covered only from a positive proving-span count", () => {
    expect(layerCoverageState(3)).toBe("covered");
    expect(layerCoverageState(1, true)).toBe("covered");
  });

  it("separates a wired-but-unexercised layer from an unwired one", () => {
    expect(layerCoverageState(0, true)).toBe("awaiting_first_event");
    expect(layerCoverageState(0, false)).toBe("not_wired");
  });

  it("maps an uncountable layer to unknown, never a fabricated not-wired", () => {
    expect(layerCoverageState(null)).toBe("unknown");
    expect(layerCoverageState(null, true)).toBe("unknown");
  });

  it("parses the gateway payload into one row per layer, in order", () => {
    const layers = parseCoverage({
      layers: [
        { layer: "llm", state: "covered", reason: "7 spans", proving_spans: 7 },
        { layer: "tools", state: "not_wired", reason: "no tool span", proving_spans: 0 },
        { layer: "account", state: "unknown", reason: "rollup is behind", proving_spans: null },
      ],
    });
    expect(layers.map((l) => l.layer)).toEqual([...COVERAGE_LAYERS]);
    expect(layers[0]).toMatchObject({ state: "covered", provingSpans: 7 });
    expect(layers[1]).toMatchObject({ state: "not_wired", provingSpans: 0 });
    // vector and embeddings were absent from the payload: reported unknown, never dropped.
    expect(layers[2]).toMatchObject({ state: "unknown", provingSpans: null });
    expect(layers[2].reason).toBeTruthy();
    expect(layers[4]).toMatchObject({ state: "unknown", provingSpans: null });
  });

  it("refuses a covered claim that carries no proving span", () => {
    const [llm] = parseCoverage({
      layers: [{ layer: "llm", state: "covered", reason: "trust me", proving_spans: 0 }],
    });
    expect(llm.state).toBe("unknown");
    expect(llm.provingSpans).toBeNull();
    expect(llm.reason).toMatch(/no span to prove it/);
  });

  it("refuses a covered claim with a missing or malformed span count", () => {
    for (const proving_spans of [null, undefined, "many", -1, Number.NaN]) {
      const [llm] = parseCoverage({
        layers: [{ layer: "llm", state: "covered", reason: "trust me", proving_spans }],
      });
      expect(llm.state).toBe("unknown");
    }
  });

  it("softens a dark layer the caller says it wired, without ever granting coverage", () => {
    const [, tools] = parseCoverage(
      {
        layers: [{ layer: "tools", state: "not_wired", reason: "no tool span", proving_spans: 0 }],
      },
      ["tools"],
    );
    expect(tools.state).toBe("awaiting_first_event");
    const [, alsoTools] = parseCoverage({ layers: [] }, ["tools"]);
    expect(alsoTools.state).toBe("unknown");
  });

  it("treats an unrecognised state as unknown rather than trusting it", () => {
    const [llm] = parseCoverage({
      layers: [{ layer: "llm", state: "definitely_fine", reason: "hi", proving_spans: 9 }],
    });
    expect(llm.state).toBe("unknown");
  });

  it("survives a garbage payload with an all-unknown report", () => {
    for (const raw of [null, undefined, {}, { layers: "nope" }, { layers: [1, 2] }]) {
      const layers = parseCoverage(raw);
      expect(layers).toHaveLength(COVERAGE_LAYERS.length);
      expect(layers.every((l) => l.state === "unknown" && l.reason)).toBe(true);
    }
  });

  it("builds an all-unknown report with the reason attached", () => {
    const layers = unknownCoverage("gateway unreachable");
    expect(layers).toHaveLength(COVERAGE_LAYERS.length);
    expect(
      layers.every((l) => l.reason === "gateway unreachable" && l.provingSpans === null),
    ).toBe(true);
  });
});
