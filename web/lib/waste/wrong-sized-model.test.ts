// SPDX-License-Identifier: Apache-2.0
// Tests for the pure wrong-sized-model detector (CTO-231, W4). These cover the detection LOGIC:
// it flags a cheaper candidate whose quality CI overlaps the incumbent (no significant regression),
// it does NOT flag a candidate the judge finds significantly worse, and it emits nothing (never a
// zero-dollar finding) when the eval is below the judged floor or the input is empty. The live
// collectWrongSizedModel wiring is exercised against the stack (numbers in the PR body).

import { describe, expect, it } from "vitest";

import {
  detectWrongSizedModel,
  type WrongSizedModelCandidate,
  type WrongSizedModelInput,
  type WrongSizedModelScope,
} from "./wrong-sized-model";

function candidate(over: Partial<WrongSizedModelCandidate> = {}): WrongSizedModelCandidate {
  return {
    candidateModel: "claude-haiku-4-5",
    provider: "anthropic",
    projectedMonthlyMicroUsd: 5_000_000_000, // cheaper than the 10B incumbent below
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
    incumbentProjectedMonthlyMicroUsd: 10_000_000_000,
    candidates: [candidate()],
    ...over,
  };
}

function input(scopes: WrongSizedModelScope[]): WrongSizedModelInput {
  return { windowDays: 30, scopes };
}

describe("detectWrongSizedModel", () => {
  it("flags a cheaper candidate whose quality CI overlaps the incumbent", () => {
    const findings = detectWrongSizedModel(input([scope()]));
    expect(findings).toHaveLength(1);
    const f = findings[0];
    expect(f.category).toBe("wrong_sized_model");
    expect(f.scopeKind).toBe("model");
    expect(f.scopeValue).toBe("claude-sonnet-4-5");
    expect(f.drillHref).toBe("/compare");
    // Candidate is half the incumbent's projected cost, so the window spend rescales to half and the
    // recoverable is the other half: $20,000 → $10,000 recoverable.
    expect(f.recoverableMicroUsd).toBe(10_000_000_000);
    expect(f.windowSpendMicroUsd).toBe(20_000_000_000);
    expect(f.evidence.incumbentModel).toBe("claude-sonnet-4-5");
    expect(f.evidence.candidateModel).toBe("claude-haiku-4-5");
    expect(f.evidence.projectedMonthlySavings).toBe(5_000_000_000);
    expect(f.evidence.qualityCiLow).toBe(0.44);
    expect(f.evidence.qualityCiHigh).toBe(0.54);
    // Fidelity caveat is carried on the reason, matching the Compare diagnostics.
    expect(f.reason).toContain("resolved-context replay, no live retrieval");
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

  it("does NOT flag when the candidate is not cheaper", () => {
    const pricey = candidate({ projectedMonthlyMicroUsd: 10_000_000_000 }); // equal to incumbent
    const findings = detectWrongSizedModel(input([scope({ candidates: [pricey] })]));
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

  it("surfaces the cheapest qualifying candidate (largest recoverable)", () => {
    const cheaper = candidate({
      candidateModel: "gemini-3-flash",
      provider: "google",
      projectedMonthlyMicroUsd: 2_500_000_000, // cheaper than the 5B haiku candidate
    });
    const findings = detectWrongSizedModel(input([scope({ candidates: [candidate(), cheaper] })]));
    expect(findings).toHaveLength(1);
    expect(findings[0].evidence.candidateModel).toBe("gemini-3-flash");
    // $20,000 rescaled to a quarter (2.5B/10B) = $5,000, recoverable $15,000.
    expect(findings[0].recoverableMicroUsd).toBe(15_000_000_000);
  });

  it("ignores a regressing candidate but still flags a qualifying cheaper one in the same scope", () => {
    const regressed = candidate({
      candidateModel: "gpt-5-mini",
      provider: "openai",
      projectedMonthlyMicroUsd: 1_000_000_000,
      winRate: 0.25,
      ciLow: 0.15,
      ciHigh: 0.35, // significantly worse — disqualified despite being cheapest
    });
    const findings = detectWrongSizedModel(input([scope({ candidates: [regressed, candidate()] })]));
    expect(findings).toHaveLength(1);
    expect(findings[0].evidence.candidateModel).toBe("claude-haiku-4-5");
  });

  it("does not flag when the incumbent has no positive projection to rescale against", () => {
    const findings = detectWrongSizedModel(
      input([scope({ incumbentProjectedMonthlyMicroUsd: 0 })]),
    );
    expect(findings).toEqual([]);
  });
});
