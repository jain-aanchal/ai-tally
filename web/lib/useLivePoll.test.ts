// SPDX-License-Identifier: Apache-2.0
// Tests for the useLivePoll hook (CTO-108).
//
// We drive the hook with fake timers + a stubbed global.fetch. The harness uses
// React's `act` via @testing-library/react's `renderHook` so timer advances and state updates
// are flushed deterministically.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLivePoll } from "./useLivePoll";

interface Payload {
  n: number;
}

function mockFetchOk(body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response);
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useLivePoll", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility("visible");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("returns initialData immediately and re-fetches on the interval", async () => {
    const fetchSpy = mockFetchOk({ n: 42 });
    vi.stubGlobal("fetch", fetchSpy);

    const { result } = renderHook(() =>
      useLivePoll<Payload>("/api/x", { n: 1 }, { intervalMs: 100 }),
    );

    // First paint = SSR data; no fetch yet.
    expect(result.current.data).toEqual({ n: 1 });
    expect(fetchSpy).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    // Let microtasks resolve (fetch().then(json).then(setState))
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.data).toEqual({ n: 42 });
    expect(result.current.updatedAt).toBeInstanceOf(Date);
    expect(result.current.error).toBeNull();
  });

  it("pauses while the tab is hidden (no fetches fired)", async () => {
    const fetchSpy = mockFetchOk({ n: 99 });
    vi.stubGlobal("fetch", fetchSpy);

    renderHook(() => useLivePoll<Payload>("/api/x", { n: 0 }, { intervalMs: 100 }));

    await act(async () => {
      setVisibility("hidden");
      await vi.advanceTimersByTimeAsync(500);
    });

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("resumes and fetches immediately when tab becomes visible", async () => {
    const fetchSpy = mockFetchOk({ n: 7 });
    vi.stubGlobal("fetch", fetchSpy);

    const { result } = renderHook(() =>
      useLivePoll<Payload>("/api/x", { n: 0 }, { intervalMs: 100 }),
    );

    await act(async () => {
      setVisibility("hidden");
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(fetchSpy).not.toHaveBeenCalled();

    await act(async () => {
      setVisibility("visible");
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchSpy).toHaveBeenCalled();
    expect(result.current.data).toEqual({ n: 7 });
  });

  it("keeps last-good data and surfaces an error when fetch fails", async () => {
    const boom = new Error("network down");
    const fetchSpy = vi.fn().mockRejectedValue(boom);
    vi.stubGlobal("fetch", fetchSpy);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const initial: Payload = { n: 5 };
    const { result } = renderHook(() =>
      useLivePoll<Payload>("/api/x", initial, { intervalMs: 100 }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.error).not.toBeNull();
    expect(result.current.data).toEqual(initial);
    expect(result.current.error?.message).toBe("network down");
    expect(warn).toHaveBeenCalled();
  });

  it("is disabled when intervalMs is 0", async () => {
    const fetchSpy = mockFetchOk({ n: 1 });
    vi.stubGlobal("fetch", fetchSpy);

    renderHook(() => useLivePoll<Payload>("/api/x", { n: 0 }, { intervalMs: 0 }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
