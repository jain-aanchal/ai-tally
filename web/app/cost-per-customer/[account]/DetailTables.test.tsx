// SPDX-License-Identifier: Apache-2.0
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BLANK } from "@/components/HonestValue";
import type { AccountFeatureCost, AccountRunCost } from "@/lib/accounts";
import { FeatureTable, RunTable } from "./DetailTables";

function feature(over: Partial<AccountFeatureCost> = {}): AccountFeatureCost {
  return { feature: "research_agent", directCostMicroUsd: 750_000, spanCount: 80, ...over };
}

function run(over: Partial<AccountRunCost> = {}): AccountRunCost {
  return {
    runId: "trace-1",
    agent: "aider",
    accountCostMicroUsd: 8_000_000,
    steps: 40,
    outcome: "success",
    ...over,
  };
}

describe("FeatureTable", () => {
  it("takes the share against the account's own spend, not the tenant's", () => {
    render(
      <FeatureTable
        features={[feature({ directCostMicroUsd: 750_000 })]}
        accountDirectMicroUsd={1_000_000}
      />,
    );
    expect(screen.getByText("75.0%")).toBeTruthy();
  });

  it("blanks the share when the account has no direct spend, rather than printing 0%", () => {
    // A share of a total that does not exist is not zero, it is unanswerable. `/compare` precedent.
    render(<FeatureTable features={[feature()]} accountDirectMicroUsd={0} />);
    const blank = screen.getByText(BLANK);
    expect(blank.closest("[title]")?.getAttribute("title")).toContain(
      "no directly attributable spend",
    );
  });

  it("says why the list is empty rather than showing a bare blank table", () => {
    render(<FeatureTable features={[]} accountDirectMicroUsd={1_000_000} />);
    expect(screen.getByText(/no spans for this account carried a feature tag/i)).toBeTruthy();
  });
});

describe("RunTable", () => {
  it("labels the cost column as this account's share and links to the whole run", () => {
    render(<RunTable runs={[run()]} />);
    // The header has to say the scope out loud: the linked page shows the WHOLE run, so a reader
    // clicking through to a bigger number needs to know that is a different scope, not a bug.
    expect(screen.getByText("Cost to this account")).toBeTruthy();
    const link = screen.getByText("trace-1").closest("a");
    expect(link?.getAttribute("href")).toBe("/agents/runs/trace-1");
  });

  it("shows the outcome inferred from the span status", () => {
    render(<RunTable runs={[run(), run({ runId: "trace-2", outcome: "failed" })]} />);
    expect(screen.getByText("success")).toBeTruthy();
    expect(screen.getByText("failed")).toBeTruthy();
  });

  it("says no runs carried this account rather than implying the account is idle", () => {
    render(<RunTable runs={[]} />);
    expect(screen.getByText(/no agent runs in the window carried this account id/i)).toBeTruthy();
  });
});
