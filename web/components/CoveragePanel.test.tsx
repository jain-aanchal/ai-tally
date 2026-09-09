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

  // #320 item 4: a layer proven by exactly one span read "1 spans".
  it("singularises a one-span count", () => {
    render(
      <CoveragePanel
        initialLayers={layers({ llm: { provingSpans: 1, reason: "1 span proves this layer" } })}
        poll={false}
      />,
    );
    expect(screen.getByText("1 span")).toBeTruthy();
    expect(screen.queryByText("1 spans")).toBeNull();
  });

  it("counts only span-proven layers in the summary", () => {
    render(<CoveragePanel initialLayers={layers()} poll={false} />);
    expect(screen.getByText(/1\/5 layers proven/)).toBeTruthy();
    expect(screen.getByText(/1 could not be read/)).toBeTruthy();
  });

  // #320: with poll off the panel is a view of what its caller polled. It used to freeze at its
  // first render's props, which put "0/5 layers proven" under a step 2 saying a trace had arrived.
  it("follows its caller's answer on a rerender when it is not polling itself", () => {
    const { rerender } = render(<CoveragePanel initialLayers={undefined} poll={false} />);
    expect(screen.getAllByText("Unknown")).toHaveLength(5);
    rerender(<CoveragePanel initialLayers={layers()} poll={false} />);
    expect(screen.getByText("Flowing")).toBeTruthy();
    expect(screen.getByText(/1\/5 layers proven/)).toBeTruthy();
  });

  it("starts unknown rather than claiming anything before the probe answers", () => {
    render(<CoveragePanel poll={false} />);
    expect(screen.getAllByText("Unknown")).toHaveLength(5);
    expect(screen.getByText(/0\/5 layers proven/)).toBeTruthy();
  });
});
