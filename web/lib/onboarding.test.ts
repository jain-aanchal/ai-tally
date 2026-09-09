// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  EXAMPLE_CREDS_NOTICE,
  type OnboardingProgress,
  activationStatus,
  deriveChecklist,
  proxyEnvSnippet,
  proxyPythonSnippet,
  provingCountLabel,
  spanCountLabel,
  traceEvidenceFromCoverage,
} from "./onboarding";

const creds = { tenantKey: "tk_test_123", proxyBaseUrl: "https://proxy.example/v1" };

function progress(over: Partial<OnboardingProgress> = {}): OnboardingProgress {
  return {
    signedUpAt: 1_000_000,
    copiedConfigAt: null,
    firstDashboardAt: null,
    ...over,
  };
}

describe("proxy snippets", () => {
  it("env snippet sets base URL + tenant key, never the OpenAI key", () => {
    const s = proxyEnvSnippet(creds);
    expect(s).toContain('OPENAI_BASE_URL="https://proxy.example/v1"');
    expect(s).toContain('TALLY_TENANT_KEY="tk_test_123"');
    expect(s.toLowerCase()).toContain("never sent to us");
    expect(s).not.toContain("OPENAI_API_KEY=");
  });

  it("python snippet wires base_url + X-Tenant-Key header", () => {
    const s = proxyPythonSnippet(creds);
    expect(s).toContain('base_url="https://proxy.example/v1"');
    expect(s).toContain('"X-Tenant-Key": "tk_test_123"');
  });
});

// #320 item 1 (second half): placeholder credentials must not read as provisioned ones. The warning
// rides in the copied text, because a developer who pasted into a terminal never sees the banner.
describe("example credentials", () => {
  it("no notice when the credentials are real", () => {
    expect(proxyEnvSnippet(creds)).not.toContain(EXAMPLE_CREDS_NOTICE);
    expect(proxyPythonSnippet(creds)).not.toContain(EXAMPLE_CREDS_NOTICE);
  });

  it("both snippets carry the placeholder notice when the values are examples", () => {
    const example = { ...creds, isExample: true };
    expect(proxyEnvSnippet(example)).toContain(EXAMPLE_CREDS_NOTICE);
    expect(proxyPythonSnippet(example)).toContain(EXAMPLE_CREDS_NOTICE);
    // As a shell/Python comment, so pasting the block still runs.
    expect(proxyEnvSnippet(example).split("\n")[0]!.startsWith("#")).toBe(true);
    expect(proxyPythonSnippet(example).split("\n")[0]!.startsWith("#")).toBe(true);
  });
});

// #320 item 4: "1 spans".
describe("spanCountLabel", () => {
  it("singularises one span and pluralises the rest", () => {
    expect(spanCountLabel(0)).toBe("0 spans");
    expect(spanCountLabel(1)).toBe("1 span");
    expect(spanCountLabel(2)).toBe("2 spans");
    expect(spanCountLabel(1200)).toBe("1,200 spans");
  });

  // #329: the panel prints all five layers' counts, and the account layer's count is rows of
  // daily_account_rollup. Its row read "5,914 spans" under a reason saying "rollup row(s)".
  it("labels the account layer's count in its own unit", () => {
    expect(provingCountLabel("account", 5_914)).toBe("5,914 rollup rows");
    expect(provingCountLabel("account", 1)).toBe("1 rollup row");
    expect(provingCountLabel("llm", 12)).toBe("12 spans");
    expect(provingCountLabel(undefined, 12)).toBe("12 spans");
  });
});

// #320 item 1: step 2 reads the coverage probe, the same evidence the panel under it renders, so
// the page can no longer say "waiting for your first trace" above a panel reporting proven layers.
describe("traceEvidenceFromCoverage", () => {
  const layer = (state: string, provingSpans: number | null, reason = "because") => ({
    layer: "llm",
    state,
    provingSpans,
    reason,
  });
  // The account layer's count is rollup rows, not spans (#329). Named separately so a test that
  // means "rollup rows" cannot accidentally read as one more span-counting layer.
  const accountLayer = (
    state: string,
    provingRows: number | null,
    reason = "rollup row(s) carry a non-empty AccountIdHash",
  ) => ({ layer: "account", state, provingSpans: provingRows, reason });

  it("reports received when a layer is proven by a span, and counts them", () => {
    const e = traceEvidenceFromCoverage([
      layer("covered", 12),
      layer("covered", 3),
      layer("not_wired", 0),
    ]);
    expect(e.state).toBe("received");
    expect(e.provingSpans).toBe(15);
    expect(e.reason).toContain("15 spans");
  });

  // #329 finding 1. The account layer's proving count comes from daily_account_rollup, one row per
  // account/day/feature/operation, not from otel_spans. Adding it to the operation layers produced
  // a total in no unit at all, printed as "N spans already prove it"; PR #325's rollup rebuild
  // moved that number by ~2,000 without a single span changing.
  it("counts spans from the operation layers only, never rollup rows", () => {
    const e = traceEvidenceFromCoverage([
      layer("covered", 12),
      layer("covered", 3),
      accountLayer("covered", 1_544_149),
    ]);
    expect(e.state).toBe("received");
    expect(e.provingSpans).toBe(15);
    expect(e.reason).toContain("15 spans across 2 layers");
    expect(e.reason).not.toContain("1,544,149");
  });

  it("reports received with no span count when only the rollup layer is covered", () => {
    // Attributed rollup rows cannot exist without spans behind them, so a trace did arrive. There
    // is still no span count to print, and printing the row count as spans is the bug.
    const e = traceEvidenceFromCoverage([
      layer("not_wired", 0),
      accountLayer("covered", 4_210),
    ]);
    expect(e.state).toBe("received");
    expect(e.provingSpans).toBeNull();
    expect(e.reason).not.toContain("4,210");
    expect(e.reason).toMatch(/no layer returned a span count/i);
  });

  it("singularises the reason for a single proving span", () => {
    const e = traceEvidenceFromCoverage([layer("covered", 1)]);
    expect(e.reason).toContain("1 span across 1 layer");
  });

  it("waits only when every layer was read and none found a span", () => {
    const e = traceEvidenceFromCoverage([layer("not_wired", 0), layer("awaiting_first_event", 0)]);
    expect(e.state).toBe("waiting");
  });

  // #329 finding 2. The mixed case: the llm layer was downgraded to unknown by parseCoverage's
  // evidence gate while tools/vector came back not_wired. "Waiting" here asserts no trace arrived
  // for a layer we could not read, while the panel below it correctly says one could not be read.
  it("does not claim waiting when some layers could not be read", () => {
    const e = traceEvidenceFromCoverage([
      { layer: "llm", state: "unknown", provingSpans: null, reason: "the probe timed out" },
      layer("not_wired", 0),
      layer("not_wired", 0),
      layer("not_wired", 0),
      accountLayer("not_wired", 0),
    ]);
    expect(e.state).toBe("unknown");
    expect(e.provingSpans).toBeNull();
    expect(e.reason).toContain("1 layer");
    expect(e.reason).toContain("the probe timed out");
  });

  it("still reports received in a mixed report when a readable layer has a span", () => {
    const e = traceEvidenceFromCoverage([
      layer("covered", 9),
      { layer: "tools", state: "unknown", provingSpans: null, reason: "the probe timed out" },
    ]);
    expect(e.state).toBe("received");
    expect(e.provingSpans).toBe(9);
  });

  it("says unknown, not waiting, when every layer came back unreadable", () => {
    const e = traceEvidenceFromCoverage([
      layer("unknown", null, "the coverage probe could not be reached"),
      layer("unknown", null, "the coverage probe could not be reached"),
    ]);
    expect(e.state).toBe("unknown");
    expect(e.provingSpans).toBeNull();
    expect(e.reason).toContain("could not be reached");
  });

  it("is unknown before the probe has answered at all", () => {
    expect(traceEvidenceFromCoverage([]).state).toBe("unknown");
  });

  it("never claims received off a covered state with no span behind it", () => {
    // parseCoverage already refuses this shape upstream; belt and braces at the read site too.
    const e = traceEvidenceFromCoverage([layer("covered", 0)]);
    expect(e.state).not.toBe("received");
  });
});

describe("checklist", () => {
  it("signed_up always done; others gated on timestamps", () => {
    const steps = deriveChecklist(progress());
    expect(steps.find((s) => s.id === "signed_up")!.done).toBe(true);
    expect(steps.find((s) => s.id === "copied_config")!.done).toBe(false);
    expect(steps.find((s) => s.id === "first_trace")!.done).toBe(false);
  });

  it("steps flip to done as progress fills in", () => {
    // #329: first_trace has no timestamp to flip it. The coverage probe is its only evidence.
    const steps = deriveChecklist(progress({ copiedConfigAt: 1_001_000 }), {
      firstTraceProven: true,
    });
    expect(steps.find((s) => s.id === "copied_config")!.done).toBe(true);
    expect(steps.find((s) => s.id === "first_trace")!.done).toBe(true);
    expect(steps.find((s) => s.id === "first_dashboard")!.done).toBe(false);
  });
});

describe("activation status", () => {
  it("not activated before a trace is proven", () => {
    const s = activationStatus(progress());
    expect(s.activated).toBe(false);
    expect(s.completedSteps).toBe(1);
  });

  it("activated on probe evidence, and reports no duration for it", () => {
    const s = activationStatus(progress(), { firstTraceProven: true });
    expect(s.activated).toBe(true);
    expect(s.completedSteps).toBe(2);
    // #329: nothing measures when the trace arrived, so there is no duration field to read and no
    // "under the 5-minute target" claim that could be built from one.
    expect(s).not.toHaveProperty("timeToFirstTraceMs");
    expect(s).not.toHaveProperty("withinTarget");
  });
});
