// SPDX-License-Identifier: Apache-2.0
// #358: the provisioning race and a genuine provisioning failure looked identical to a customer,
// because neither was caught at all. These tests pin the distinction the recovery screen draws.

import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  PROVISIONING_WAIT_MS,
  WorkspaceProvisioning,
  provisioningScreen,
} from "./WorkspaceProvisioning";

describe("provisioningScreen", () => {
  it("says the workspace is still being set up inside the wait window", () => {
    const s = provisioningScreen("pending", 4_000, null);
    expect(s.waiting).toBe(true);
    expect(s.heading).toMatch(/Setting up your workspace/);
  });

  // The bounded half of "bounded retry". Past the window we stop implying it is nearly done, since
  // a wait this long is no longer the race, and a customer parked on a spinner is told nothing.
  it("stops claiming progress once the wait window has passed", () => {
    const s = provisioningScreen("pending", PROVISIONING_WAIT_MS + 1_000, null);
    expect(s.waiting).toBe(false);
    expect(s.heading).toMatch(/has not finished being set up/);
    expect(s.body).toMatch(/contact support/);
  });

  // A non-404 resolution failure is not the race. It does not self-heal, so it is reported at once
  // with the reason rather than hidden behind another 40 seconds of "nearly there".
  it("reports a real failure immediately, with its reason and no waiting", () => {
    const s = provisioningScreen(
      "failed",
      1_000,
      "the control plane answered HTTP 503 when we asked which workspace this organization belongs to",
    );
    expect(s.waiting).toBe(false);
    expect(s.heading).toMatch(/could not be set up/);
    expect(s.body).toMatch(/HTTP 503/);
    expect(s.body).toMatch(/will not clear on its own/);
  });
});

describe("WorkspaceProvisioning", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("polls and switches to the failure copy when the poll says failed", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ state: "failed", reason: "the control plane answered HTTP 503" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WorkspaceProvisioning pollMs={5} />);
    expect(screen.getByText(/Setting up your workspace/)).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByText(/could not be set up/)).toBeTruthy();
    });
    expect(screen.getByText(/HTTP 503/)).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith("/api/tenant/provisioning-status", {
      cache: "no-store",
    });
  });

  it("keeps waiting rather than reporting a failure when the poll request itself drops", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network")));
    render(<WorkspaceProvisioning pollMs={5} />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
    // A dropped request is not evidence about provisioning either way.
    expect(screen.getByText(/Setting up your workspace/)).toBeTruthy();
    expect(screen.queryByText(/could not be set up/)).toBeNull();
  });
});
