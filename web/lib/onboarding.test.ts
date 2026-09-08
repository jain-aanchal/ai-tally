// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import {
  EXAMPLE_CREDS_NOTICE,
  TIME_TO_FIRST_TRACE_TARGET_MS,
  type OnboardingProgress,
  activationStatus,
  deriveChecklist,
  formatDuration,
  proxyEnvSnippet,
  proxyPythonSnippet,
  spanCountLabel,
  timeToFirstTraceMs,
  traceEvidenceFromCoverage,
} from "./onboarding";

const creds = { tenantKey: "tk_test_123", proxyBaseUrl: "https://proxy.example/v1" };

function progress(over: Partial<OnboardingProgress> = {}): OnboardingProgress {
  return {
    signedUpAt: 1_000_000,
    copiedConfigAt: null,
    firstTraceAt: null,
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
});

// #320 item 1: step 2 reads the coverage probe, the same evidence the panel under it renders, so
// the page can no longer say "waiting for your first trace" above a panel reporting proven layers.
describe("traceEvidenceFromCoverage", () => {
  const layer = (state: string, provingSpans: number | null, reason = "because") => ({
    state,
    provingSpans,
    reason,
  });

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

  it("singularises the reason for a single proving span", () => {
    const e = traceEvidenceFromCoverage([layer("covered", 1)]);
    expect(e.reason).toContain("1 span across 1 layer");
  });

  it("waits only when the probe actually read a layer and found no span", () => {
    const e = traceEvidenceFromCoverage([layer("not_wired", 0), layer("awaiting_first_event", 0)]);
    expect(e.state).toBe("waiting");
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
    const steps = deriveChecklist(
      progress({ copiedConfigAt: 1_001_000, firstTraceAt: 1_002_000 }),
    );
    expect(steps.find((s) => s.id === "copied_config")!.done).toBe(true);
    expect(steps.find((s) => s.id === "first_trace")!.done).toBe(true);
    expect(steps.find((s) => s.id === "first_dashboard")!.done).toBe(false);
  });
});

describe("activation status", () => {
  it("not activated before first trace", () => {
    const s = activationStatus(progress());
    expect(s.activated).toBe(false);
    expect(s.timeToFirstTraceMs).toBeNull();
    expect(s.completedSteps).toBe(1);
  });

  it("activated + within target when trace arrives quickly", () => {
    const s = activationStatus(progress({ firstTraceAt: 1_000_000 + 30_000 }));
    expect(s.activated).toBe(true);
    expect(s.withinTarget).toBe(true);
    expect(s.timeToFirstTraceMs).toBe(30_000);
  });

  it("activated but over target when trace is late", () => {
    const late = 1_000_000 + TIME_TO_FIRST_TRACE_TARGET_MS + 1;
    const s = activationStatus(progress({ firstTraceAt: late }));
    expect(s.activated).toBe(true);
    expect(s.withinTarget).toBe(false);
  });
});

describe("timeToFirstTraceMs", () => {
  it("null without a trace; clamped non-negative", () => {
    expect(timeToFirstTraceMs(progress())).toBeNull();
    expect(timeToFirstTraceMs(progress({ firstTraceAt: 999_999 }))).toBe(0);
  });
});

describe("formatDuration", () => {
  it("renders ms, seconds, and minutes", () => {
    expect(formatDuration(800)).toBe("800ms");
    expect(formatDuration(3_400)).toBe("3.4s");
    expect(formatDuration(130_000)).toBe("2m 10s");
  });
});
