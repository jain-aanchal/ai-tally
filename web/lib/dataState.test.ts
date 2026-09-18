// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import {
  allZero,
  asOfLabel,
  ageMs,
  boundaryFromMinutesAgo,
  deriveDataState,
  isSentinelBoundary,
  isStale,
  relativeAge,
  someZero,
  STALE_AFTER_MS,
  zeroEnabledLayers,
} from "./dataState";

const NOW = Date.parse("2026-05-30T12:00:00Z");

describe("boundaryFromMinutesAgo", () => {
  it("produces a boundary that reads fresh when within the 2h window", () => {
    const boundary = boundaryFromMinutesAgo(23, NOW);
    expect(isStale(boundary, NOW)).toBe(false);
    expect(deriveDataState({ isEmpty: false, isPartial: false, reconciledThrough: boundary, now: NOW })).toBe("fresh");
  });
  it("produces a boundary that reads stale past the 2h window", () => {
    const boundary = boundaryFromMinutesAgo(3 * 60, NOW); // 3h ago
    expect(isStale(boundary, NOW)).toBe(true);
    expect(deriveDataState({ isEmpty: false, isPartial: false, reconciledThrough: boundary, now: NOW })).toBe("stale");
  });
  it("stale outranks partial (never present stale as fresh)", () => {
    const boundary = boundaryFromMinutesAgo(180, NOW);
    expect(deriveDataState({ isEmpty: false, isPartial: true, reconciledThrough: boundary, now: NOW })).toBe("stale");
  });
  it("maps null (unknown reconciler last-run, CTO-169) to the epoch sentinel: no as-of, no stale badge", () => {
    const boundary = boundaryFromMinutesAgo(null, NOW);
    expect(isSentinelBoundary(boundary)).toBe(true);
    expect(asOfLabel(boundary)).toBeNull();
    expect(isStale(boundary, NOW)).toBe(false);
  });
});

describe("isSentinelBoundary", () => {
  it("treats the 1970 epoch sentinel as pre-data, not a real boundary", () => {
    expect(isSentinelBoundary("1970-01-01")).toBe(true);
  });
  it("treats unparseable timestamps as sentinel", () => {
    expect(isSentinelBoundary("not-a-date")).toBe(true);
  });
  it("treats a recent real boundary as non-sentinel", () => {
    expect(isSentinelBoundary("2026-05-30T10:00:00Z")).toBe(false);
  });
});

describe("isStale", () => {
  it("a boundary older than 2h is stale", () => {
    const boundary = new Date(NOW - STALE_AFTER_MS - 60_000).toISOString();
    expect(isStale(boundary, NOW)).toBe(true);
  });
  it("a boundary within 2h is fresh", () => {
    const boundary = new Date(NOW - 60 * 60 * 1000).toISOString();
    expect(isStale(boundary, NOW)).toBe(false);
  });
  it("the 1970 sentinel is never stale (it's empty, not stale)", () => {
    expect(isStale("1970-01-01", NOW)).toBe(false);
  });
});

describe("deriveDataState precedence", () => {
  it("empty wins over everything", () => {
    expect(
      deriveDataState({ isEmpty: true, isPartial: true, reconciledThrough: "1970-01-01", now: NOW }),
    ).toBe("empty");
  });
  it("the 1970 sentinel with no data is empty, not stale", () => {
    expect(
      deriveDataState({ isEmpty: true, isPartial: false, reconciledThrough: "1970-01-01", now: NOW }),
    ).toBe("empty");
  });
  it("stale outranks partial", () => {
    const old = new Date(NOW - STALE_AFTER_MS - 1).toISOString();
    expect(deriveDataState({ isEmpty: false, isPartial: true, reconciledThrough: old, now: NOW })).toBe(
      "stale",
    );
  });
  it("partial when fresh but missing a source", () => {
    const fresh = new Date(NOW - 1000).toISOString();
    expect(deriveDataState({ isEmpty: false, isPartial: true, reconciledThrough: fresh, now: NOW })).toBe(
      "partial",
    );
  });
  it("fresh when populated and recent", () => {
    const fresh = new Date(NOW - 1000).toISOString();
    expect(deriveDataState({ isEmpty: false, isPartial: false, reconciledThrough: fresh, now: NOW })).toBe(
      "fresh",
    );
  });
  it("fresh when no boundary is provided and data is full", () => {
    expect(deriveDataState({ isEmpty: false, isPartial: false })).toBe("fresh");
  });
});

describe("allZero / someZero", () => {
  it("allZero true for all-zero record", () => {
    expect(allZero({ a: 0, b: 0 })).toBe(true);
  });
  it("allZero true for empty record", () => {
    expect(allZero({})).toBe(true);
  });
  it("allZero false when any value is non-zero", () => {
    expect(allZero({ a: 0, b: 1 })).toBe(false);
  });
  it("someZero true for mixed record", () => {
    expect(someZero({ a: 0, b: 1 })).toBe(true);
  });
  it("someZero false for all-zero record", () => {
    expect(someZero({ a: 0, b: 0 })).toBe(false);
  });
  it("someZero false for all-non-zero record", () => {
    expect(someZero({ a: 2, b: 1 })).toBe(false);
  });
});

describe("labels", () => {
  it("asOfLabel returns the boundary for a real timestamp", () => {
    expect(asOfLabel("2026-05-30")).toBe("2026-05-30");
  });
  it("asOfLabel returns null for the sentinel", () => {
    expect(asOfLabel("1970-01-01")).toBeNull();
  });
  it("relativeAge reads in hours", () => {
    const boundary = new Date(NOW - 3 * 60 * 60 * 1000).toISOString();
    expect(relativeAge(boundary, NOW)).toBe("3h ago");
  });
  it("ageMs is positive for past boundaries", () => {
    expect(ageMs("2026-05-30T10:00:00Z", NOW)).toBeGreaterThan(0);
  });
});

describe("zeroEnabledLayers (CTO-107, on span counts since CTO-431)", () => {
  it("stays silent when only the enabled layer has data", () => {
    // Typed as a variable, the way a caller holds it: layers beyond `enabled` are fine.
    const spans = { llm: 100, vector: 0 };
    expect(zeroEnabledLayers(spans, ["llm"])).toEqual([]);
  });
  it("fires for an enabled layer that delivered nothing", () => {
    expect(zeroEnabledLayers({ llm: 100, vector: 0 }, ["llm", "vector"])).toEqual(["vector"]);
  });
  it("stays silent when every enabled layer has data", () => {
    expect(zeroEnabledLayers({ llm: 100, vector: 100 }, ["llm", "vector"])).toEqual([]);
  });
  it("returns empty when no connectors are declared (LLM-only demos)", () => {
    expect(zeroEnabledLayers({}, [])).toEqual([]);
  });
  it("ignores layers that aren't declared, even when silent", () => {
    // tools sent nothing but was never declared: by-design partial, not a real gap.
    const spans = { llm: 5, vector: 0, tools: 0 };
    expect(zeroEnabledLayers(spans, ["llm"])).toEqual([]);
  });
});

// CTO-431. The banner this feeds says "that connector isn't producing data right now", a claim about
// SPANS, and it was answered with COST. sum() skips NULLs, so a layer whose spans all lack a catalog
// rate summed to 0 and was named as a dead connector, three lines under a tile already saying that
// sum was unknown. Sending a customer to debug what is working is worse than an unexplained blank.
//
// The counts are now the ONLY input: cost is not a parameter, so the old reading is unreachable
// rather than merely unused, and a caller without counts has to decide for itself whether to make
// the claim (both call sites gate, and the page tests below pin what they decided).
describe("zeroEnabledLayers reads delivery, not spend (CTO-431)", () => {
  it("does not blame a connector that delivered spans we could not price", () => {
    // 260 vector spans arrived and not one could be priced, so the cost is 0 and the connector is
    // plainly alive. Under the old signature this was the input that produced ["vector"].
    expect(zeroEnabledLayers({ llm: 40, vector: 260 }, ["llm", "vector"])).toEqual([]);
  });

  it("still names a connector that really delivered nothing", () => {
    // The correction must not swallow a genuine outage: no spans is the finding it exists for.
    expect(zeroEnabledLayers({ llm: 40, vector: 0 }, ["llm", "vector"])).toEqual(["vector"]);
  });

  it("cannot be handed a cost to be misled by", () => {
    // Regression guard on the SHAPE, not just the values. The defect was a caller passing per-layer
    // COST into this position, which type-checked because both are Record<Layer, number>. The only
    // defence left is that there is now exactly one numeric argument and its name says spans, so
    // this pins the arity: a third argument would not compile.
    expect(zeroEnabledLayers.length).toBe(2);
  });
});
