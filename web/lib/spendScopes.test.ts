// SPDX-License-Identifier: Apache-2.0
// The scope vocabulary (CTO-211). Short file, but the key round trip is load-bearing: it is what a
// `?scope=` in the URL means, and getting it wrong selects a different slice of spend than the one
// the heading names.

import { describe, expect, it } from "vitest";

import {
  parseScopeKey,
  scopeKey,
  scopeLabel,
  scopeOfBudget,
  TENANT_SCOPE,
} from "./spendScopes";

describe("scopeKey / parseScopeKey", () => {
  it("round trips every kind", () => {
    for (const scope of [
      TENANT_SCOPE,
      { kind: "feature" as const, value: "research-agent" },
      { kind: "model" as const, value: "gpt-4o" },
      { kind: "layer" as const, value: "compute" },
    ]) {
      expect(parseScopeKey(scopeKey(scope))).toEqual(scope);
    }
  });

  it("splits on the FIRST colon only, so a model id keeps its own colons", () => {
    // Splitting on all of them would silently truncate the scope to a prefix, which selects a
    // different slice of spend than the URL names.
    const scope = { kind: "model" as const, value: "bedrock:anthropic.claude-3" };
    expect(scopeKey(scope)).toBe("model:bedrock:anthropic.claude-3");
    expect(parseScopeKey("model:bedrock:anthropic.claude-3")).toEqual(scope);
  });

  it("returns null rather than falling back to tenant-wide for anything it cannot parse", () => {
    // Null, so the caller falls back explicitly and says it did. A silent swap to tenant-wide puts
    // whole-tenant numbers under a heading naming one feature.
    for (const bad of ["", "  ", "feature", "feature:", ":x", "provider:openai", "tenant:x", null]) {
      expect(parseScopeKey(bad)).toBeNull();
    }
  });
});

describe("scopeLabel", () => {
  it("names the whole tenant rather than an empty value", () => {
    expect(scopeLabel(TENANT_SCOPE)).toBe("Whole tenant");
    expect(scopeLabel({ kind: "feature", value: "research-agent" })).toBe(
      "feature: research-agent",
    );
  });
});

describe("scopeOfBudget", () => {
  it("reads a budget row's scope, and rejects rows this dashboard cannot forecast", () => {
    expect(scopeOfBudget({ scope_kind: "tenant", scope_value: "" })).toEqual(TENANT_SCOPE);
    expect(scopeOfBudget({ scope_kind: "feature", scope_value: "a" })).toEqual({
      kind: "feature",
      value: "a",
    });
    // A scoped budget naming nothing is not a scope; it is a row that should never have been
    // written, and forecasting it as tenant-wide would report the wrong dollars against it.
    expect(scopeOfBudget({ scope_kind: "feature", scope_value: "" })).toBeNull();
    expect(scopeOfBudget({ scope_kind: "provider", scope_value: "openai" })).toBeNull();
  });
});
