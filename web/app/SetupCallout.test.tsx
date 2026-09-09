// SPDX-License-Identifier: Apache-2.0
// #358: the setup page was unreachable. These tests pin the two things that make it reachable:
// the Home callout renders for a tenant with no data (and, separately, for one whose data we could
// not check), and the shell carries a permanent nav entry pointing at it.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SetupCallout, setupCalloutCopy } from "./SetupCallout";

describe("SetupCallout", () => {
  it("says nothing once a span has landed", () => {
    expect(setupCalloutCopy("connected")).toBeNull();
    const { container } = render(<SetupCallout status="connected" />);
    expect(container.innerHTML).toBe("");
  });

  it("points a tenant with no telemetry at setup", () => {
    render(<SetupCallout status="waiting" />);
    expect(screen.getByText(/No telemetry has arrived yet/)).toBeTruthy();
    const link = screen.getByRole("link", { name: /Finish setup/ });
    expect(link.getAttribute("href")).toBe("/onboarding");
  });

  // The honest-under-uncertainty case. A probe we could not read is not evidence that the tenant is
  // new, so the callout must not claim it is: it says we could not tell, and still offers the link.
  it("does not claim a tenant is new when the probe could not be read", () => {
    render(<SetupCallout status="unknown" />);
    expect(screen.getByText(/could not tell whether your telemetry has arrived/)).toBeTruthy();
    expect(screen.queryByText(/No telemetry has arrived yet/)).toBeNull();
    expect(screen.getByRole("link", { name: /Open setup/ }).getAttribute("href")).toBe("/onboarding");
  });
});

describe("shell reachability", () => {
  // A source-text guard, the same shape as the FINAL guard in clickhouse.test.ts. Rendering the
  // shell would prove the link exists in one pathname state; what actually regressed here is the
  // nav table, so the test reads the nav table.
  it("keeps a nav entry pointing at /onboarding", () => {
    const src = readFileSync(resolve(__dirname, "../components/Shell.tsx"), "utf8");
    expect(src).toMatch(/href:\s*"\/onboarding"/);
  });
});
