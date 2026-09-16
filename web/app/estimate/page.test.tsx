// SPDX-License-Identifier: Apache-2.0
// CTO-298 follow-up, the render half. There was no test for this page, which is how the bug below
// shipped green: every route-level assertion passed while the page drew the wrong thing from them.
//
// The bug: the empty state was derived from `current.monthlyCostMicroUsd === null`, and `current`
// is filled in by the fixture alone, so the test held for EVERY real tenant. A pilot with a real
// replay corpus opened /estimate and was told "no telemetry has reached ai-tally for this
// workspace", as a measured fact, by a route that had queried neither spend nor traffic. That is
// the same invariant violation as the original ticket, pointing the other way. And because the
// what-if form, the KPI tiles, the drivers and every carefully worded blank live inside `body`,
// a real tenant never saw any of them.
//
// So the three cases that matter are the three a real deployment produces, plus the demo path.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ apiGet: vi.fn() }));

import EstimatePage from "./page";
import { apiGet } from "@/lib/api";
import { EMPTY_PROJECTION, projection as fixture, type Projection } from "@/lib/estimate";

const mockApiGet = apiGet as unknown as ReturnType<typeof vi.fn>;

async function renderPage(payload: Projection) {
  mockApiGet.mockResolvedValueOnce(payload);
  render(await EstimatePage());
}

/** A real tenant whose workspace has traffic, and a replayed corpus behind the what-if. */
const WITH_CORPUS: Projection = {
  ...EMPTY_PROJECTION,
  workspaceTraffic: "connected",
  proposed: { monthlyCostMicroUsd: 5_000_000, p99CostMicroUsd: null, meanLatencyMs: null },
  sample: { ...EMPTY_PROJECTION.sample, used: 60 },
};

/** The same tenant before anything has been sent: the probe ran and found no span. */
const NO_TRAFFIC: Projection = { ...EMPTY_PROJECTION, workspaceTraffic: "waiting" };

/** The probe itself could not run. Whether data exists is genuinely unknown. */
const PROBE_FAILED: Projection = { ...EMPTY_PROJECTION, workspaceTraffic: "unknown" };

/** The "nothing has arrived" claim, which only `waiting` earns. */
const NOTHING_ARRIVED = /no telemetry has reached ai-tally for this workspace/i;

describe("/estimate renders the state the route actually measured", () => {
  it("gives a real tenant with a corpus the what-if, not an empty state", async () => {
    await renderPage(WITH_CORPUS);

    // The regression: this page was unconditionally the empty state for every signed-in tenant.
    expect(screen.queryByText(NOTHING_ARRIVED)).toBeNull();
    // The what-if and its honest blanks are on the product path rather than dead code behind a
    // gate no real tenant could pass.
    expect(screen.getByRole("button", { name: "Estimate" })).toBeTruthy();
    expect(screen.getByText("Driver breakdown")).toBeTruthy();
  });

  it("tells a tenant with no traffic exactly what the probe found, and nothing more", async () => {
    await renderPage(NO_TRAFFIC);

    // `waiting` is the probe having run and found no span, so this copy is a report, not a guess.
    expect(screen.getByText(NOTHING_ARRIVED)).toBeTruthy();
    expect(screen.getByText(/the first-event probe found no spans/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Estimate" })).toBeNull();
  });

  it("says the source could not be read, and does not claim there is no data", async () => {
    await renderPage(PROBE_FAILED);

    // The distinction the whole invariant rests on: "we could not look" is not "there is nothing".
    expect(screen.getByText(/Source unavailable/i)).toBeTruthy();
    expect(screen.getByText(/we cannot tell whether any traffic has arrived/i)).toBeTruthy();
    expect(screen.queryByText(NOTHING_ARRIVED)).toBeNull();
  });

  it("still labels the demo fixture as sample data and renders its body", async () => {
    await renderPage(fixture);

    expect(screen.getByText("Sample data")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Estimate" })).toBeTruthy();
    expect(screen.queryByText(NOTHING_ARRIVED)).toBeNull();
  });
});

describe("the samples-used diagnostic", () => {
  it("is an explained blank when no replay was attempted, never a literal 0", async () => {
    await renderPage({ ...WITH_CORPUS, sample: { ...WITH_CORPUS.sample, used: null } });

    // It rendered `0` off EMPTY_PROJECTION, which reads as "we replayed and used none of it".
    expect(
      screen.getByText(/no replay has been attempted for this workload/i),
    ).toBeTruthy();
  });

  it("still prints a real zero when a replay ran and matched nothing", async () => {
    await renderPage({ ...WITH_CORPUS, sample: { ...WITH_CORPUS.sample, used: 0 } });

    // A measured zero is a measurement and stays a number; only the unmeasured one is a blank.
    expect(screen.getByText("0")).toBeTruthy();
    expect(screen.queryByText(/no replay has been attempted for this workload/i)).toBeNull();
  });
});
