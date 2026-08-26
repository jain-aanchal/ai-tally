// SPDX-License-Identifier: Apache-2.0
// FilterBar + useFilters wiring (CTO-221, D1). next/navigation is mocked so the bar can be driven
// without a router: we assert that interacting with it writes the expected URL through
// router.replace and that it renders the state parsed back out of the query string.

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
let currentQuery = "";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/cost",
  useSearchParams: () => new URLSearchParams(currentQuery),
}));

import { FilterBar } from "./FilterBar";

afterEach(() => {
  replace.mockClear();
  currentQuery = "";
});

describe("<FilterBar />", () => {
  it("defaults to the 30d preset and writes a preset change to the URL", () => {
    render(<FilterBar />);
    expect(screen.getByRole("button", { name: "30d" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    // 7d is non-default, so it lands in the query; the default group-by stays out.
    expect(replace).toHaveBeenCalledWith("/cost?range=7d", { scroll: false });
  });

  it("returns to a clean URL when the default preset is chosen", () => {
    currentQuery = "range=7d";
    render(<FilterBar />);
    fireEvent.click(screen.getByRole("button", { name: "30d" }));
    expect(replace).toHaveBeenCalledWith("/cost", { scroll: false });
  });

  it("preserves an unrelated ?tag= when a filter changes", () => {
    currentQuery = "tag=research_agent";
    render(<FilterBar options={{ provider: [{ value: "openai" }, { value: "anthropic" }] }} />);
    fireEvent.click(screen.getByRole("button", { name: /Provider/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /openai/ }));
    expect(replace).toHaveBeenCalledTimes(1);
    const url = replace.mock.calls[0][0] as string;
    expect(url).toContain("tag=research_agent");
    expect(url).toContain("provider=openai");
  });

  it("changes the group-by dimension", () => {
    render(<FilterBar />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "provider" } });
    expect(replace).toHaveBeenCalledWith("/cost?groupBy=provider", { scroll: false });
  });

  it("only renders filter controls for dimensions it was given options for", () => {
    render(<FilterBar options={{ provider: [{ value: "openai" }] }} />);
    expect(screen.getByRole("button", { name: /Provider/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Account/ })).toBeNull();
  });
});
