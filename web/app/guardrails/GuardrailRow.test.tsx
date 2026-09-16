// SPDX-License-Identifier: Apache-2.0
// CTO-393: a guardrail edit that did not reach the control plane must not read as applied.
//
// The row is optimistic: it moves the control, then POSTs. It checked only `res.ok`, so the
// unreachable branch (200 with `persisted: false`) left the new mode on screen under
// "Mode -> ... Live within the refresh window." while live traffic ran on the old rule.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GuardrailRule } from "@/lib/guardrails";
import { GuardrailRow } from "./GuardrailRow";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

const RULE: GuardrailRule = {
  id: "gr_1",
  scopeKind: "agent",
  scope: "research_agent",
  mode: "observe",
  maxCostMicroUsd: 1_000_000,
  maxSteps: null,
  wouldHaveFiredThisWeek: 3,
  runsThisWeek: 100,
};

function renderRow() {
  return render(
    <table>
      <tbody>
        <GuardrailRow initialRule={RULE} />
      </tbody>
    </table>,
  );
}

function stubPost(status: number, payload: unknown) {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify(payload), { status }),
  ) as unknown as typeof fetch;
}

/** Flipping the mode saves immediately, which is the shortest path to the save branch. */
function flipMode() {
  fireEvent.change(screen.getByLabelText(/Mode for research_agent/i), {
    target: { value: "warn" },
  });
}

describe("GuardrailRow save reporting (CTO-393)", () => {
  it("reports a 503 from an unreachable control plane instead of 'Live within the refresh window'", async () => {
    stubPost(503, { error: "Not saved: the control plane is unreachable.", persisted: false });
    renderRow();

    flipMode();

    await waitFor(() => expect(screen.getByText(/Not saved/i)).toBeTruthy());
    expect(screen.queryByText(/Live within the refresh window/i)).toBeNull();
  });

  it("treats a 200 that says persisted:false as a failure, not a save", async () => {
    // The echo the dev path still returns. It is not a claim that anything was stored, so the row
    // must not dress it up as one.
    stubPost(200, { rule: RULE, changeId: "c1", persisted: false });
    renderRow();

    flipMode();

    await waitFor(() => expect(screen.getByText(/Not saved/i)).toBeTruthy());
    expect(screen.queryByText(/Live within the refresh window/i)).toBeNull();
  });

  it("still confirms a real save", async () => {
    stubPost(200, { rule: RULE, changeId: "c1", persisted: true });
    renderRow();

    flipMode();

    await waitFor(() =>
      expect(screen.getByText(/Live within the refresh window/i)).toBeTruthy(),
    );
    expect(screen.queryByText(/Not saved/i)).toBeNull();
  });
});
