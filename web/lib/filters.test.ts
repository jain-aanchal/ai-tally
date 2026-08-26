// SPDX-License-Identifier: Apache-2.0
// Tests for the URL-synced filter state (CTO-221, D1). The load-bearing property is the round-trip:
// parse(serialize(state)) === state for any normalized state, which is what makes a shared link
// reconstruct the exact view. The other half is that serialization never clobbers ?tag= / ?scope=.

import { describe, expect, it } from "vitest";

import {
  DEFAULT_GROUP_BY,
  DEFAULT_RANGE_PRESET,
  type FilterState,
  clearAllFilters,
  defaultFilterState,
  filtersToSearchParams,
  isIsoDate,
  parseFilters,
  parseMulti,
  rangeDays,
  toggleDimensionValue,
  withCustomRange,
  withGroupBy,
  withRangePreset,
} from "./filters";

function sp(query: string): URLSearchParams {
  return new URLSearchParams(query);
}

describe("parseFilters", () => {
  it("returns the documented defaults for an empty query", () => {
    const s = parseFilters(sp(""));
    expect(s.range.preset).toBe(DEFAULT_RANGE_PRESET);
    expect(s.groupBy).toBe(DEFAULT_GROUP_BY);
    expect(s.filters).toEqual({
      feature: [],
      model: [],
      layer: [],
      provider: [],
      account: [],
    });
  });

  it("parses presets, group-by, and multi-select filters", () => {
    const s = parseFilters(sp("range=90d&groupBy=provider&provider=openai,anthropic&model=gpt-4o"));
    expect(s.range.preset).toBe("90d");
    expect(s.groupBy).toBe("provider");
    expect(s.filters.provider).toEqual(["openai", "anthropic"]);
    expect(s.filters.model).toEqual(["gpt-4o"]);
  });

  it("degrades an unknown group-by and range to the defaults rather than throwing", () => {
    const s = parseFilters(sp("range=all-time&groupBy=galaxy"));
    expect(s.range.preset).toBe(DEFAULT_RANGE_PRESET);
    expect(s.groupBy).toBe(DEFAULT_GROUP_BY);
  });

  it("honors a valid custom range and rejects a malformed or reversed one", () => {
    const ok = parseFilters(sp("range=custom&from=2026-06-01&to=2026-06-30"));
    expect(ok.range).toEqual({ preset: "custom", from: "2026-06-01", to: "2026-06-30" });

    // Reversed → not a range we can honor, so it degrades to the default preset.
    const reversed = parseFilters(sp("range=custom&from=2026-06-30&to=2026-06-01"));
    expect(reversed.range.preset).toBe(DEFAULT_RANGE_PRESET);

    // Missing / garbage end → default preset.
    const missing = parseFilters(sp("range=custom&from=2026-06-01"));
    expect(missing.range.preset).toBe(DEFAULT_RANGE_PRESET);
    const garbage = parseFilters(sp("range=custom&from=2026-13-40&to=2026-06-01"));
    expect(garbage.range.preset).toBe(DEFAULT_RANGE_PRESET);
  });
});

describe("parseMulti", () => {
  it("splits, trims, drops blanks and dedupes while preserving order", () => {
    expect(parseMulti(" a , b ,,a, c ")).toEqual(["a", "b", "c"]);
    expect(parseMulti(null)).toEqual([]);
    expect(parseMulti("")).toEqual([]);
  });
});

describe("isIsoDate", () => {
  it("accepts real calendar dates and rejects impossible ones", () => {
    expect(isIsoDate("2026-02-28")).toBe(true);
    expect(isIsoDate("2026-02-31")).toBe(false);
    expect(isIsoDate("2026-13-01")).toBe(false);
    expect(isIsoDate("not-a-date")).toBe(false);
    expect(isIsoDate(null)).toBe(false);
  });
});

describe("filtersToSearchParams", () => {
  it("omits every default so the canonical view has a clean URL", () => {
    expect(filtersToSearchParams(defaultFilterState()).toString()).toBe("");
  });

  it("serializes non-default range, group-by and filters", () => {
    let s = defaultFilterState();
    s = withRangePreset(s, "7d");
    s = withGroupBy(s, "provider");
    s = toggleDimensionValue(s, "provider", "openai");
    const out = filtersToSearchParams(s);
    expect(out.get("range")).toBe("7d");
    expect(out.get("groupBy")).toBe("provider");
    expect(out.get("provider")).toBe("openai");
  });

  it("preserves ?tag= and ?scope= and any other unrelated param (never clobbers them)", () => {
    const base = sp("tag=research_agent&scope=model:gpt-4o&debug=1");
    let s = defaultFilterState();
    s = withGroupBy(s, "layer");
    s = toggleDimensionValue(s, "feature", "chatbot");
    const out = filtersToSearchParams(s, base);
    expect(out.get("tag")).toBe("research_agent");
    expect(out.get("scope")).toBe("model:gpt-4o");
    expect(out.get("debug")).toBe("1");
    expect(out.get("feature")).toBe("chatbot");
  });

  it("rewrites its own keys without leaving stale copies from the base URL", () => {
    // A base carrying an old provider filter must be overwritten, not appended to.
    const base = sp("provider=stale&groupBy=account");
    const out = filtersToSearchParams(defaultFilterState(), base);
    expect(out.getAll("provider")).toEqual([]);
    expect(out.get("groupBy")).toBeNull();
  });
});

describe("round-trip parse(serialize(state)) === state", () => {
  const cases: FilterState[] = [
    defaultFilterState(),
    withRangePreset(defaultFilterState(), "7d"),
    withRangePreset(defaultFilterState(), "90d"),
    withGroupBy(defaultFilterState(), "account"),
    withCustomRange(defaultFilterState(), "2026-06-01", "2026-06-30"),
    (() => {
      let s = defaultFilterState();
      s = withRangePreset(s, "90d");
      s = withGroupBy(s, "model");
      s = toggleDimensionValue(s, "provider", "openai");
      s = toggleDimensionValue(s, "provider", "anthropic");
      s = toggleDimensionValue(s, "feature", "chatbot");
      return s;
    })(),
  ];

  it.each(cases.map((c, i) => [i, c] as const))("case %i", (_i, state) => {
    const query = filtersToSearchParams(state);
    expect(parseFilters(query)).toEqual(state);
  });

  it("also round-trips when other params ride alongside", () => {
    let s = defaultFilterState();
    s = withGroupBy(s, "provider");
    s = toggleDimensionValue(s, "model", "gpt-4o");
    const query = filtersToSearchParams(s, sp("tag=chatbot&scope=layer:llm"));
    // Re-parsing only reads the managed keys; tag/scope are ignored by the parser but preserved.
    expect(parseFilters(query)).toEqual(s);
    expect(query.get("tag")).toBe("chatbot");
  });
});

describe("state helpers", () => {
  it("toggleDimensionValue adds then removes", () => {
    let s = defaultFilterState();
    s = toggleDimensionValue(s, "layer", "llm");
    expect(s.filters.layer).toEqual(["llm"]);
    s = toggleDimensionValue(s, "layer", "llm");
    expect(s.filters.layer).toEqual([]);
  });

  it("withCustomRange rejects an invalid or reversed pair", () => {
    const s = defaultFilterState();
    expect(withCustomRange(s, "2026-06-30", "2026-06-01")).toBe(s);
    expect(withCustomRange(s, "garbage", "2026-06-01")).toBe(s);
  });

  it("clearAllFilters empties every dimension but keeps range and group-by", () => {
    let s = withGroupBy(defaultFilterState(), "model");
    s = toggleDimensionValue(s, "provider", "openai");
    const cleared = clearAllFilters(s);
    expect(cleared.filters.provider).toEqual([]);
    expect(cleared.groupBy).toBe("model");
  });
});

describe("rangeDays", () => {
  it("returns fixed spans for presets and an inclusive count for custom", () => {
    expect(rangeDays({ preset: "7d", from: null, to: null })).toBe(7);
    expect(rangeDays({ preset: "30d", from: null, to: null })).toBe(30);
    expect(rangeDays({ preset: "90d", from: null, to: null })).toBe(90);
    expect(rangeDays({ preset: "custom", from: "2026-06-01", to: "2026-06-30" })).toBe(30);
    expect(rangeDays({ preset: "custom", from: "2026-06-01", to: "2026-06-01" })).toBe(1);
  });
});
