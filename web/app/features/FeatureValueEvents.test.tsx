// SPDX-License-Identifier: Apache-2.0
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FeatureValueEvents } from "./FeatureValueEvents";
import type { FeatureEconomics } from "@/lib/features";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

function feature(overrides: Partial<FeatureEconomics>): FeatureEconomics {
  return {
    feature: "smart_search",
    valueEvent: null,
    costPerUserMicroUsd: 20_000,
    valuePerUserMicroUsd: null,
    paybackDays: null,
    attributionRate: null,
    conversions: 0,
    attributionBreakdown: { direct: 0, sessionStitched: 0, identityGraphStitched: 0, unmatched: 0 },
    ...overrides,
  };
}

const CONFIGURED = feature({
  feature: "research_agent",
  valueEvent: "subscription_created",
  valuePerUserMicroUsd: 1_400_000,
});

function mockFetch(handlers: {
  observed?: { name: string; count: number }[];
  observedAvailable?: boolean;
  postOk?: boolean;
  /** CTO-393: what the save route says it actually stored. Defaults to a real save. */
  postPersisted?: boolean;
  postStatus?: number;
}) {
  const post = vi.fn();
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (init?.method === "POST") {
      post(JSON.parse(String(init.body)));
      return new Response(
        JSON.stringify({
          feature: "smart_search",
          eventName: "paid_conversion",
          persisted: handlers.postPersisted ?? true,
        }),
        {
          status:
            handlers.postStatus ?? (handlers.postOk === false ? 500 : 200),
        },
      );
    }
    if (url.includes("/api/features/value-events")) {
      return new Response(
        JSON.stringify({
          observedEvents: handlers.observed ?? [],
          observedAvailable: handlers.observedAvailable ?? true,
          configured: [],
        }),
        { status: 200 },
      );
    }
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
  return { post };
}

describe("FeatureValueEvents (CTO-140)", () => {
  it("shows the Finish setup banner when a feature has no value event", () => {
    mockFetch({});
    render(<FeatureValueEvents initialFeatures={[CONFIGURED, feature({})]} />);
    expect(screen.getByText(/Finish setup/i)).toBeTruthy();
    expect(screen.getByText(/no value event yet/i)).toBeTruthy();
  });

  it("opens the modal listing observed business events", async () => {
    mockFetch({
      observed: [
        { name: "paid_conversion", count: 42 },
        { name: "signup", count: 900 },
      ],
    });
    render(<FeatureValueEvents initialFeatures={[feature({})]} />);
    fireEvent.click(screen.getByText(/configure value event/i));
    await waitFor(() => expect(screen.getByText("paid_conversion")).toBeTruthy());
    expect(screen.getByText("signup")).toBeTruthy();
    expect(screen.getByText(/42 events/i)).toBeTruthy();
  });

  it("renders the honest-empty state when business_events has zero rows", async () => {
    mockFetch({ observed: [], observedAvailable: true });
    render(<FeatureValueEvents initialFeatures={[feature({})]} />);
    fireEvent.click(screen.getByText(/configure value event/i));
    await waitFor(() =>
      expect(screen.getByText(/No business events yet: wire Stripe/i)).toBeTruthy(),
    );
  });

  it("saves the selected event and shows it in the row", async () => {
    const { post } = mockFetch({ observed: [{ name: "paid_conversion", count: 42 }] });
    render(<FeatureValueEvents initialFeatures={[feature({})]} />);
    fireEvent.click(screen.getByText(/configure value event/i));
    await waitFor(() => expect(screen.getByText("paid_conversion")).toBeTruthy());

    fireEvent.click(screen.getByRole("radio", { name: /paid_conversion/i }));
    fireEvent.click(screen.getByRole("button", { name: /Save value event/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith({ feature: "smart_search", eventName: "paid_conversion" }),
    );
    // Row now shows the saved event and the banner is gone.
    await waitFor(() => expect(screen.getByText("paid_conversion")).toBeTruthy());
    expect(screen.queryByText(/Finish setup/i)).toBeNull();
  });

  // CTO-393: the modal checked only `res.ok`, so an unreachable control plane closed it and marked
  // the feature configured while nothing had been stored. Whether a feature has a value event is
  // what decides if its ROI is attributed at all, so a failed save has to stay visible.
  it.each([
    ["a 503 from an unreachable control plane", { postStatus: 503, postPersisted: false }],
    ["a 200 that says persisted:false", { postStatus: 200, postPersisted: false }],
  ])("keeps the modal open and reports the failure on %s", async (_name, overrides) => {
    mockFetch({ observed: [{ name: "paid_conversion", count: 42 }], ...overrides });
    render(<FeatureValueEvents initialFeatures={[feature({})]} />);
    fireEvent.click(screen.getByText(/configure value event/i));
    await waitFor(() => expect(screen.getByText("paid_conversion")).toBeTruthy());

    fireEvent.click(screen.getByRole("radio", { name: /paid_conversion/i }));
    fireEvent.click(screen.getByRole("button", { name: /Save value event/i }));

    await waitFor(() => expect(screen.getByText(/Not saved/i)).toBeTruthy());
    // The row was never updated, so the workspace still shows this feature as unconfigured.
    expect(screen.getByText(/Finish setup/i)).toBeTruthy();
  });
});
