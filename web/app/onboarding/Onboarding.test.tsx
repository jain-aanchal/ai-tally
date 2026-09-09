// SPDX-License-Identifier: Apache-2.0
// #320 item 1. The papercut was the page arguing with itself: step 2 rendered "Waiting for your
// first trace…" directly above a coverage panel reporting layers a span had already proven, because
// the two read different sources. These assertions pin the contradiction shut from the rendered
// output, which is where it was visible, and keep the third state honest: a probe we could not read
// must not be reported as "no trace yet".

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Onboarding } from "./Onboarding";
import { unknownCoverage, type LayerCoverage } from "@/lib/firstEvent";
import type { OnboardingProgress, TenantProxyCredentials } from "@/lib/onboarding";

const creds: TenantProxyCredentials = {
  tenantKey: "tk_example_replace_me",
  proxyBaseUrl: "https://proxy.example.ai-tally.dev/v1",
  isExample: true,
};

const progress: OnboardingProgress = {
  signedUpAt: 1_000_000,
  copiedConfigAt: null,
  firstDashboardAt: null,
};

/** Coverage where the LLM layer is proven by spans and the rest are dark. */
function coveredLayers(spans = 9): LayerCoverage[] {
  return [
    { layer: "llm", state: "covered", reason: `${spans} spans prove this layer`, provingSpans: spans },
    { layer: "tools", state: "not_wired", reason: "not wired: no tool span", provingSpans: 0 },
    { layer: "vector", state: "not_wired", reason: "not wired: no vector span", provingSpans: 0 },
    { layer: "embeddings", state: "not_wired", reason: "not wired: no embedding span", provingSpans: 0 },
    { layer: "account", state: "not_wired", reason: "not wired: no account rollup", provingSpans: 0 },
  ];
}

function waitingLayers(): LayerCoverage[] {
  return coveredLayers().map((l) =>
    l.layer === "llm"
      ? { ...l, state: "awaiting_first_event", reason: "wired, awaiting first event", provingSpans: 0 }
      : l,
  );
}

beforeEach(() => {
  // The component's own poll: default to the unknown answer so a test that does not care about the
  // network is not silently seeded with coverage.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ layers: unknownCoverage("no probe") }) })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("onboarding step 2", () => {
  it("does not say it is waiting when the coverage panel reports proven spans", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, json: async () => ({ layers: coveredLayers() }) })),
    );
    render(<Onboarding initialProgress={progress} creds={creds} initialLayers={coveredLayers()} />);
    expect(screen.queryByText(/Waiting for your first trace/)).toBeNull();
    expect(screen.getByText(/First trace received/)).toBeTruthy();
    // The very thing the panel below is showing, said in step 2's own words: one count, two places.
    expect(screen.getAllByText("9 spans").length).toBe(2);
    // ...and the panel is still there reporting the same layer.
    await waitFor(() => expect(screen.getByText("LLM calls")).toBeTruthy());
    expect(screen.getByText("Flowing")).toBeTruthy();
  });

  // The contradiction the ticket is about, asserted end to end: whatever the poll returns, step 2's
  // claim and the panel's summary line move together. Mount starts unknown; the poll then answers.
  it("step 2 and the panel below it never disagree once the poll answers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, json: async () => ({ layers: coveredLayers(4) }) })),
    );
    render(<Onboarding initialProgress={progress} creds={creds} />);
    await waitFor(() => expect(screen.getByText(/First trace received/)).toBeTruthy());
    expect(screen.getByText(/1\/5 layers proven/)).toBeTruthy();
    expect(screen.queryByText(/0\/5 layers proven/)).toBeNull();
    expect(screen.getAllByText("4 spans").length).toBe(2);
  });

  it("waits only when the probe read the layers and found no span", () => {
    render(<Onboarding initialProgress={progress} creds={creds} initialLayers={waitingLayers()} />);
    expect(screen.getByText(/Waiting for your first trace/)).toBeTruthy();
  });

  it("renders the honest blank, not a definite 'no trace', when the probe is unreadable", () => {
    const reason = "the coverage probe could not be reached, so we could not read this layer";
    const { container } = render(
      <Onboarding initialProgress={progress} creds={creds} initialLayers={unknownCoverage(reason)} />,
    );
    expect(screen.queryByText(/Waiting for your first trace/)).toBeNull();
    expect(screen.getByText(/We cannot tell whether a trace has arrived/)).toBeTruthy();
    expect(container.querySelector(`[title="${reason}"]`)).toBeTruthy();
  });

  it("withholds a time-to-first-trace it never measured", () => {
    render(<Onboarding initialProgress={progress} creds={creds} initialLayers={coveredLayers()} />);
    // The probe proves a trace arrived; it does not say WHEN. #329: no path measures the arrival at
    // all now, so the duration and its 5-minute verdict are gone rather than permanently blank.
    expect(screen.queryByText(/under the 5-minute target/)).toBeNull();
    expect(screen.queryByText(/^in /)).toBeNull();
  });

  // #329 finding 3. #320 deleted the old poll and the "Send a test trace" button and put nothing on
  // the write side, so the funnel stopped recording first_trace entirely. The probe transition
  // reports it now, flagged `noticed` because the probe cannot say when the trace arrived.
  it("reports first_trace to the funnel as a noticed stage, once", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url) === "/api/onboarding" && init?.method === "POST") {
          posts.push(JSON.parse(String(init.body)));
          return new Response(JSON.stringify({ event: null }), { status: 200 });
        }
        return new Response(JSON.stringify({ layers: coveredLayers() }), { status: 200 });
      }),
    );
    render(<Onboarding initialProgress={progress} creds={creds} initialLayers={coveredLayers()} />);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ stage: "first_trace", noticed: true });
  });

  it("reports nothing to the funnel while the probe cannot be read", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url) === "/api/onboarding" && init?.method === "POST") {
          posts.push(JSON.parse(String(init.body)));
          return new Response(JSON.stringify({ event: null }), { status: 200 });
        }
        return new Response(JSON.stringify({ layers: unknownCoverage("probe down") }), {
          status: 200,
        });
      }),
    );
    render(
      <Onboarding
        initialProgress={progress}
        creds={creds}
        initialLayers={unknownCoverage("probe down")}
      />,
    );
    await waitFor(() => expect(screen.getByText(/We cannot tell whether a trace/)).toBeTruthy());
    expect(posts).toHaveLength(0);
  });

  // The right-rail checklist used to track its own client-side flag, so it could disagree with step
  // 2 as well. It follows the probe now: signup plus a proven first trace is 2 of 4.
  it("flips the checklist step from the same evidence", async () => {
    const { container } = render(
      <Onboarding initialProgress={progress} creds={creds} initialLayers={coveredLayers()} />,
    );
    await waitFor(() =>
      expect(
        Array.from(container.querySelectorAll("div")).some(
          (n) => n.textContent === "2/4 complete",
        ),
      ).toBe(true),
    );
  });
});

describe("onboarding step 1", () => {
  it("labels placeholder credentials unmistakably, on the page and in the snippet", () => {
    render(<Onboarding initialProgress={progress} creds={creds} initialLayers={coveredLayers()} />);
    expect(screen.getByTestId("example-creds-notice").textContent).toMatch(/EXAMPLE VALUES/i);
    expect(screen.getByText(/# Example values, not this tenant's provisioned credentials/)).toBeTruthy();
  });

  it("shows no such banner once the credentials are real", () => {
    render(
      <Onboarding
        initialProgress={progress}
        creds={{ tenantKey: "tk_live_abc", proxyBaseUrl: "https://proxy.ai-tally.dev/v1" }}
        initialLayers={coveredLayers()}
      />,
    );
    expect(screen.queryByTestId("example-creds-notice")).toBeNull();
  });
});
