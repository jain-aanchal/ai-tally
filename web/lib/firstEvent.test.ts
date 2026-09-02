// SPDX-License-Identifier: Apache-2.0
// First-data onboarding status mapper (Initiative 2, §9).
import { describe, expect, it } from "vitest";

import { firstEventStatus } from "./firstEvent";

describe("firstEventStatus", () => {
  it("maps a found row to connected", () => {
    expect(firstEventStatus(true)).toBe("connected");
  });

  it("maps a ran-but-empty probe to waiting", () => {
    expect(firstEventStatus(false)).toBe("waiting");
  });

  it("maps an unrunnable probe to unknown, never a fabricated waiting", () => {
    // null = ClickHouse unreachable. Honest under uncertainty: not collapsed into a definite "no".
    expect(firstEventStatus(null)).toBe("unknown");
  });
});
