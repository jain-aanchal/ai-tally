import { describe, expect, it } from "vitest";

import {
  TIME_TO_FIRST_TRACE_TARGET_MS,
  type OnboardingProgress,
  activationStatus,
  deriveChecklist,
  formatDuration,
  proxyEnvSnippet,
  proxyPythonSnippet,
  timeToFirstTraceMs,
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
