// SPDX-License-Identifier: Apache-2.0
// CTO-393: the threshold panel must not say "saved" over cutoffs that were never stored.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_THRESHOLDS } from "@/lib/unitEconomics";
import { ThresholdSettings } from "./ThresholdSettings";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

function stubPost(status: number, payload: unknown) {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify(payload), { status }),
  ) as unknown as typeof fetch;
}

function renderPanel() {
  render(
    <ThresholdSettings
      initial={DEFAULT_THRESHOLDS}
      defaults={DEFAULT_THRESHOLDS}
      hasOverride={false}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Save thresholds/i }));
}

describe("ThresholdSettings save reporting (CTO-393)", () => {
  it("surfaces an unreachable control plane rather than setting 'saved'", async () => {
    stubPost(503, { error: "Not saved: the control plane is unreachable.", persisted: false });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/Not saved/i)).toBeTruthy());
    expect(screen.queryByText(/saved ✓/)).toBeNull();
  });

  it("treats a 200 that says persisted:false as a failure", async () => {
    stubPost(200, { thresholds: DEFAULT_THRESHOLDS, changeId: "c1", persisted: false });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/Not saved/i)).toBeTruthy());
    expect(screen.queryByText(/saved ✓/)).toBeNull();
  });

  it("still confirms a real save", async () => {
    stubPost(200, { thresholds: DEFAULT_THRESHOLDS, changeId: "c1", persisted: true });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/saved ✓/)).toBeTruthy());
    expect(screen.queryByText(/Not saved/i)).toBeNull();
  });
});
