// SPDX-License-Identifier: Apache-2.0
// Initiative 2 §9 review: the Copy button must report success ONLY on a real copy (finding #10) and
// the first-event badge must stop polling once connected (finding #11).

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectPanel } from "./ConnectPanel";

// The badge polls /api/onboarding/first-event. Default the fetch to "waiting" so a test that only
// cares about the Copy button is not perturbed by the poll; individual tests override it.
function stubFetch(status: "waiting" | "connected" | "unknown") {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ status }),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", {
    value,
    configurable: true,
    writable: true,
  });
}

describe("ConnectPanel Copy button (finding #10)", () => {
  it("reports Copied only after writeText resolves", async () => {
    stubFetch("waiting");
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText });

    render(<ConnectPanel token="tally_sk_live_test" />);
    const copy = screen.getAllByRole("button", { name: "Copy" })[0];
    fireEvent.click(copy);

    await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy());
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("tally_sk_live_test"));
  });

  it("reports Copy failed when the Clipboard API is absent", async () => {
    stubFetch("waiting");
    setClipboard(undefined);

    render(<ConnectPanel token="tally_sk_live_test" />);
    const copy = screen.getAllByRole("button", { name: "Copy" })[0];
    fireEvent.click(copy);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copy failed" })).toBeTruthy(),
    );
    // The false-success claim must never appear on this path.
    expect(screen.queryByRole("button", { name: "Copied" })).toBeNull();
  });

  it("reports Copy failed when writeText rejects", async () => {
    stubFetch("waiting");
    setClipboard({ writeText: vi.fn().mockRejectedValue(new Error("denied")) });

    render(<ConnectPanel token="tally_sk_live_test" />);
    const copy = screen.getAllByRole("button", { name: "Copy" })[0];
    fireEvent.click(copy);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copy failed" })).toBeTruthy(),
    );
  });
});

describe("ConnectPanel first-event badge (finding #11)", () => {
  it("stops polling once the status is connected", async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch("connected");
    setClipboard({ writeText: vi.fn().mockResolvedValue(undefined) });

    render(<ConnectPanel token="tally_sk_live_test" />);

    // First run is SSR-only (no immediate fetch); the first poll fires one interval in and resolves
    // to "connected", which latches the badge and disables further polling.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(screen.getByText(/We received your first event\./i)).toBeTruthy();
    const callsAtConnect = fetchMock.mock.calls.length;
    expect(callsAtConnect).toBeGreaterThan(0);

    // Well past several more intervals: no additional polls once connected.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000 * 5);
    });
    expect(fetchMock.mock.calls.length).toBe(callsAtConnect);
  });
});
