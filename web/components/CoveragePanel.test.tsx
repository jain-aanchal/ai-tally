// SPDX-License-Identifier: Apache-2.0
// Coverage panel (CTO-261, onboarding-agent §4.1 / §7). The assertions that matter are the honest
// ones: a dark layer is named with its reason, and a layer we could not read renders the blank with
// that reason rather than a zero.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BLANK } from "./HonestValue";
import { CoveragePanel } from "./CoveragePanel";
import type { LayerCoverage } from "@/lib/firstEvent";

function layers(overrides: Partial<Record<string, Partial<LayerCoverage>>> = {}): LayerCoverage[] {
  const base: LayerCoverage[] = [
    { layer: "llm", state: "covered", reason: "12 spans prove this layer", provingSpans: 12 },
    {
      layer: "tools",
      state: "awaiting_first_event",
      reason: "wired, awaiting first event: no span with GenAiOperation = 'tool'",
      provingSpans: 0,
    },
    {
      layer: "vector",
      state: "not_wired",
      reason: "not wired: no span with GenAiOperation = 'vector'",
      provingSpans: 0,
    },
    {
      layer: "embeddings",
      state: "not_wired",
      reason: "not wired: no span with GenAiOperation = 'embeddings'",
      provingSpans: 0,
    },
    {
      layer: "account",
      state: "unknown",
      reason: "could not read daily_account_rollup, so we cannot tell",
      provingSpans: null,
    },
  ];
  return base.map((l) => ({ ...l, ...(overrides[l.layer] ?? {}) }));
}

describe("CoveragePanel", () => {
  it("shows each layer with its state badge", () => {
    render(<CoveragePanel initialLayers={layers()} poll={false} />);
    expect(screen.getByText("LLM calls")).toBeTruthy();
    expect(screen.getByText("Flowing")).toBeTruthy();
    expect(screen.getByText("Wired, awaiting first event")).toBeTruthy();
    expect(screen.getAllByText("Not wired")).toHaveLength(2);
    expect(screen.getByText("Unknown")).toBeTruthy();
  });

  it("names every dark layer with its reason", () => {
    render(<CoveragePanel initialLayers={layers()} poll={false} />);
    expect(screen.getByText(/no span with GenAiOperation = 'vector'/)).toBeTruthy();
    expect(screen.getByText(/wired, awaiting first event/)).toBeTruthy();
  });

  it("renders a blank with the reason, not a zero, for a layer it could not read", () => {
    const { container } = render(<CoveragePanel initialLayers={layers()} poll={false} />);
    const blank = container.querySelector("[title*='could not read daily_account_rollup']");
    expect(blank).toBeTruthy();
    expect(blank?.textContent).toContain(BLANK);
    // Only the three layers whose count is a KNOWN zero print one. The unknown layer prints no
    // number at all, because a zero there would be a fabricated fact rather than a measured one.
    expect(screen.getAllByText("0 spans")).toHaveLength(3);
  });

  it("shows the proving span count behind a covered layer", () => {
    render(<CoveragePanel initialLayers={layers()} poll={false} />);
    expect(screen.getByText("12 spans")).toBeTruthy();
  });

  it("counts only span-proven layers in the summary", () => {
    render(<CoveragePanel initialLayers={layers()} poll={false} />);
    expect(screen.getByText(/1\/5 layers proven by a span/)).toBeTruthy();
    expect(screen.getByText(/1 could not be read/)).toBeTruthy();
  });

  it("starts unknown rather than claiming anything before the probe answers", () => {
    render(<CoveragePanel poll={false} />);
    expect(screen.getAllByText("Unknown")).toHaveLength(5);
    expect(screen.getByText(/0\/5 layers proven by a span/)).toBeTruthy();
  });
});
