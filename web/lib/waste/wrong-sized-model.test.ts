// SPDX-License-Identifier: Apache-2.0
// Tests for the pure wrong-sized-model detector (CTO-231, W4; per-call basis CTO-236). These cover the
// detection LOGIC on the per-call basis: it flags a candidate that is cheaper PER CALL and whose
// quality CI overlaps the incumbent (no significant regression), it does NOT flag a candidate the
// judge finds significantly worse, it skips a candidate that is really the incumbent (same base id),
// and it emits nothing (never a zero-dollar finding) when the eval is below the judged floor or the
// input is empty. The live collectWrongSizedModel wiring is exercised against the stack (numbers in
// the PR body); on this demo there is no captured replay corpus + eval pass, so it honestly returns [].

import { describe, expect, it } from "vitest";

import {
  baseModelId,
  detectWrongSizedModel,
  type WrongSizedModelCandidate,
  type WrongSizedModelInput,
  type WrongSizedModelScope,
} from "./wrong-sized-model";

function candidate(over: Partial<WrongSizedModelCandidate> = {}): WrongSizedModelCandidate {
  return {
    candidateModel: "claude-haiku-4-5",
    provider: "anthropic",
    perCallMicroUsd: 5_000, // cheaper per call than the 10,000 incumbent below
    winRate: 0.49, // just below even, but CI overlaps 0.5 → no significant regression
    ciLow: 0.44,
    ciHigh: 0.54,
    samplesJudged: 40,
    samplesReplayed: 120,
    ...over,
  };
}

function scope(over: Partial<WrongSizedModelScope> = {}): WrongSizedModelScope {
  return {
    scopeKind: "model",
    scopeValue: "claude-sonnet-4-5",
    incumbentModel: "claude-sonnet-4-5",
    windowSpendMicroUsd: 20_000_000_000, // $20,000 observed over the window
    incumbentPerCallMicroUsd: 10_000, // measured avg cost per call on real traffic
    candidates: [candidate()],
    ...over,
  };
}

function input(scopes: WrongSizedModelScope[]): WrongSizedModelInput {
  return { windowDays: 30, scopes };
}

describe("baseModelId", () => {
  it("strips dated / versioned suffixes so ids for the same model compare equal", () => {
    expect(baseModelId("claude-sonnet-4-5-20250219")).toBe("claude-sonnet-4-5");
    expect(baseModelId("gpt-5-mini-2025-01-01")).toBe("gpt-5-mini");
    expect(baseModelId("claude-haiku-4-5")).toBe("claude-haiku-4-5");
    expect(baseModelId("claude-sonnet-4-5-20250219")).toBe(baseModelId("claude-sonnet-4-5"));
  });
});

describe("detectWrongSizedModel", () => {
  it("flags a candidate cheaper per call whose quality CI overlaps the incumbent", () => {
    const findings = detectWrongSizedModel(input([scope()]));
    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("wrong_sized_model");
    expect(f.scopeKind).toBe("model");
    expect(f.scopeValue).toBe("claude-sonnet-4-5");
    expect(f.drillHref).toBe("/compare");
    // Candidate is half the incumbent's per-call cost, so the window spend rescales to half and the
    // recoverable is the other half: $20,000 → $10,000 recoverable. recoverable = spend*(1 - 5000/10000).
    expect(f.recoverableMicroUsd).toBe(10_000_000_000);
    expect(f.windowSpendMicroUsd).toBe(20_000_000_000);
    expect(f.evidence.incumbentModel).toBe("claude-sonnet-4-5");
    expect(f.evidence.candidateModel).toBe("claude-haiku-4-5");
    expect(f.evidence.incumbentPerCall).toBe(10_000);
    expect(f.evidence.candidatePerCall).toBe(5_000);
    expect(f.evidence.perCallSavings).toBe(5_000);
    expect(f.evidence.qualityCiLow).toBe(0.44);
    expect(f.evidence.qualityCiHigh).toBe(0.54);
    expect(f.evidence.samplesReplayed).toBe(120);
    // The honest caveat: candidate cost from resolved-context replay, incumbent from real traffic,
    // compared per call.
    expect(f.reason).toContain("resolved-context replay, no live retrieval");
    expect(f.reason).toContain("per call");
    expect(f.reason).toContain("measured on");
  });

  it("reports high confidence on a tight CI and medium on a wide one", () => {
    const tight = detectWrongSizedModel(input([scope()]));
    expect(tight[0].confidence).toBe("high"); // width 0.10

    const wide = detectWrongSizedModel(
      input([scope({ candidates: [candidate({ ciLow: 0.35, ciHigh: 0.6 })] })]),
    );
    expect(wide[0].confidence).toBe("medium"); // width 0.25
  });

  it("does NOT flag when the candidate quality significantly regresses (CI below the even line)", () => {
    const regressed = candidate({ winRate: 0.3, ciLow: 0.2, ciHigh: 0.42 }); // ciHigh < 0.5
    const findings = detectWrongSizedModel(input([scope({ candidates: [regressed] })]));
    expect(findings).toEqual([]);
  });

  it("does NOT flag when the candidate is not cheaper per call", () => {
    const pricey = candidate({ perCallMicroUsd: 10_000 }); // equal to incumbent per call
    const findings = detectWrongSizedModel(input([scope({ candidates: [pricey] })]));
    expect(findings).toEqual([]);
  });

  it("skips a candidate that is really the incumbent (same base id), even dated", () => {
    // Candidate carries a dated suffix of the incumbent id: it IS the incumbent, so it is skipped
    // regardless of any (spurious) per-call difference the replay would show.
    const self = candidate({ candidateModel: "claude-sonnet-4-5-20250219", perCallMicroUsd: 1 });
    const findings = detectWrongSizedModel(input([scope({ candidates: [self] })]));
    expect(findings).toEqual([]);
  });

  it("returns nothing (never a zero-dollar finding) when eval is below the judged floor", () => {
    const belowFloor = candidate({ samplesJudged: 4 }); // < MIN_JUDGED_SAMPLES
    const findings = detectWrongSizedModel(input([scope({ candidates: [belowFloor] })]));
    expect(findings).toEqual([]);
  });

  it("returns nothing when replay is below the replayed floor", () => {
    const thinReplay = candidate({ samplesReplayed: 10 }); // < MIN_REPLAYED_SAMPLES
    const findings = detectWrongSizedModel(input([scope({ candidates: [thinReplay] })]));
    expect(findings).toEqual([]);
  });

  it("returns [] for empty input, not a fabricated finding", () => {
    expect(detectWrongSizedModel(input([]))).toEqual([]);
    expect(detectWrongSizedModel(input([scope({ candidates: [] })]))).toEqual([]);
  });

  it("surfaces the cheapest-per-call qualifying candidate (largest recoverable)", () => {
    const cheaper = candidate({
      candidateModel: "gemini-3-flash",
      provider: "google",
      perCallMicroUsd: 2_500, // cheaper per call than the 5,000 haiku candidate
    });
    const findings = detectWrongSizedModel(input([scope({ candidates: [candidate(), cheaper] })]));
    expect(findings).toHaveLength(1);
    expect(findings[0].evidence.candidateModel).toBe("gemini-3-flash");
    // $20,000 rescaled to a quarter (2500/10000) = $5,000, recoverable $15,000.
    expect(findings[0].recoverableMicroUsd).toBe(15_000_000_000);
  });

  it("ignores a regressing candidate but still flags a qualifying cheaper one in the same scope", () => {
    const regressed = candidate({
      candidateModel: "gpt-5-mini",
      provider: "openai",
      perCallMicroUsd: 1_000,
      winRate: 0.25,
      ciLow: 0.15,
      ciHigh: 0.35, // significantly worse - disqualified despite being cheapest per call
    });
    const findings = detectWrongSizedModel(input([scope({ candidates: [regressed, candidate()] })]));
    expect(findings).toHaveLength(1);
    expect(findings[0].evidence.candidateModel).toBe("claude-haiku-4-5");
  });

  it("does not flag when the incumbent has no positive per-call cost to rescale against", () => {
    const findings = detectWrongSizedModel(input([scope({ incumbentPerCallMicroUsd: 0 })]));
    expect(findings).toEqual([]);
  });

  it("scopes to a feature when one is set (feature scopeKind, feature in the reason)", () => {
    const findings = detectWrongSizedModel(
      input([scope({ scopeKind: "feature", scopeValue: "chatbot", feature: "chatbot" })]),
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].scopeKind).toBe("feature");
    expect(findings[0].reason).toContain("on chatbot");
  });

  it("emits one finding per feature scope, each with its OWN feature + incumbent (CTO-238)", () => {
    // Two features, each with a different incumbent and a different cheaper candidate. The detector
    // must not collapse them: one finding per scope, scoped to that feature's own incumbent.
    const research = scope({
      scopeKind: "feature",
      scopeValue: "research_agent",
      feature: "research_agent",
      incumbentModel: "claude-sonnet-4-5",
      incumbentPerCallMicroUsd: 10_000,
      candidates: [candidate({ candidateModel: "claude-haiku-4-5", perCallMicroUsd: 5_000 })],
    });
    const chatbot = scope({
      scopeKind: "feature",
      scopeValue: "chatbot",
      feature: "chatbot",
      incumbentModel: "gpt-4o",
      incumbentPerCallMicroUsd: 8_000,
      candidates: [
        candidate({ candidateModel: "gpt-5-mini", provider: "openai", perCallMicroUsd: 2_000 }),
      ],
    });
    const findings = detectWrongSizedModel(input([research, chatbot]));
    expect(findings).toHaveLength(2);

    const byFeature = new Map(findings.map((f) => [f.scopeValue, f]));
    const r = byFeature.get("research_agent")!;
    expect(r.scopeKind).toBe("feature");
    expect(r.evidence.incumbentModel).toBe("claude-sonnet-4-5");
    expect(r.evidence.candidateModel).toBe("claude-haiku-4-5");
    expect(r.reason).toContain("on research_agent");

    const c = byFeature.get("chatbot")!;
    expect(c.scopeKind).toBe("feature");
    expect(c.evidence.incumbentModel).toBe("gpt-4o");
    expect(c.evidence.candidateModel).toBe("gpt-5-mini");
    expect(c.reason).toContain("on chatbot");
  });
});
