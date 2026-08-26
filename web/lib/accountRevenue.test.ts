// SPDX-License-Identifier: Apache-2.0
// CTO-196: revenue per account, and the null-vs-zero distinction the margin column depends on.

import { describe, expect, it } from "vitest";

import {
  REVENUE_WINDOW_SQL,
  UNATTRIBUTED_ACCOUNT,
  accountRevenueFromRow,
  accountRevenueReport,
  accountRevenueSql,
  revenueForAccount,
  type AccountRevenueSqlRow,
} from "./accountRevenue";
import { DEFAULT_REVENUE_POLICY, type RevenuePolicy } from "./revenueSources";

const ACCT_A = "a".repeat(64);
const ACCT_B = "b".repeat(64);

function row(overrides: Partial<AccountRevenueSqlRow> = {}): AccountRevenueSqlRow {
  return {
    account_id_hash: ACCT_A,
    gross_micro: "0",
    refund_micro: "0",
    revenue_events: "0",
    distinct_users: "0",
    ...overrides,
  };
}

describe("null vs zero", () => {
  it("returns null, not 0, for an account with no revenue events", () => {
    // The whole point of the ticket. 0 would read as "this customer generates no revenue", which
    // is a claim we cannot support; null reads as "we do not know", which is the truth.
    const rec = accountRevenueFromRow(row({ revenue_events: "0" }));
    expect(rec.revenueMicroUsd).toBeNull();
    expect(rec.revenueMicroUsd).not.toBe(0);
  });

  it("treats an account with only `count` engagement events as unknown", () => {
    // `count` events carry no amount. An account that has them and nothing else has produced
    // sums of 0, which must not be mistaken for measured zero revenue.
    const rec = accountRevenueFromRow(
      row({ gross_micro: "0", refund_micro: "0", revenue_events: "0", distinct_users: "12" }),
    );
    expect(rec.revenueMicroUsd).toBeNull();
    expect(rec.distinctUsers).toBe(12);
  });

  it("keeps a genuine measured zero as 0", () => {
    // A charge fully refunded is knowledge, not absence. This is the other half of the
    // distinction: null and 0 must both be reachable and must mean different things.
    const rec = accountRevenueFromRow(
      row({ gross_micro: "5000000", refund_micro: "5000000", revenue_events: "2" }),
    );
    expect(rec.revenueMicroUsd).toBe(0);
    expect(rec.revenueMicroUsd).not.toBeNull();
  });

  it("returns null for an account the report has no row for", () => {
    const report = accountRevenueReport([
      row({ account_id_hash: ACCT_A, gross_micro: "100", revenue_events: "1" }),
    ]);
    expect(revenueForAccount(report, ACCT_B)).toBeNull();
    expect(revenueForAccount(null, ACCT_A)).toBeNull();
    expect(revenueForAccount(report, ACCT_A)).toBe(100);
  });

  it("passes a null revenue through the lookup rather than coercing it", () => {
    const report = accountRevenueReport([row({ account_id_hash: ACCT_A, revenue_events: "0" })]);
    expect(revenueForAccount(report, ACCT_A)).toBeNull();
  });
});

describe("refunds net off", () => {
  it("subtracts refunds from gross", () => {
    const rec = accountRevenueFromRow(
      row({ gross_micro: "1200000", refund_micro: "200000", revenue_events: "3" }),
    );
    expect(rec.revenueMicroUsd).toBe(1_000_000);
    expect(rec.grossMicroUsd).toBe(1_200_000);
    expect(rec.refundMicroUsd).toBe(200_000);
  });

  it("lets refunds exceeding gross go negative rather than clamping", () => {
    // A net negative account is a real finding (a churned customer refunded last month's
    // invoice). Clamping to 0 would hide it from the profitability ranking.
    const rec = accountRevenueFromRow(
      row({ gross_micro: "0", refund_micro: "750000", revenue_events: "1" }),
    );
    expect(rec.revenueMicroUsd).toBe(-750_000);
  });
});

describe("accountRevenueSql", () => {
  const sqlOf = (p: RevenuePolicy = DEFAULT_REVENUE_POLICY) => accountRevenueSql(p).sql;

  it("zero-fills every sumIf over the Nullable(Int64) amount", () => {
    // The NULL trap E1 hit: sumIf over a Nullable column returns NULL for a group with no matching
    // row, and NULL minus anything is NULL, so on a tenant with zero refunds the refund term
    // swallowed the entire subtraction. Every sumIf argument must be ifNull(..., 0).
    const sql = sqlOf();
    const sumIfs = sql.match(/sumIf\([^)]*\)*/g) ?? [];
    expect(sumIfs.length).toBeGreaterThanOrEqual(2);
    for (const frag of sumIfs) {
      expect(frag).toContain("ifNull(");
    }
    expect(sql).not.toMatch(/sumIf\(\s*b\.ValueAmountMicro/);
    expect(sql).not.toMatch(/sumIf\(\s*abs\(b\.ValueAmountMicro/);
  });

  it("uses the same calendar-aligned window as the cost queries", () => {
    expect(REVENUE_WINDOW_SQL).toBe("toDate(now()) - INTERVAL 29 DAY");
    expect(sqlOf()).toContain("b.OccurredAt >= toDate(now()) - INTERVAL 29 DAY");
  });

  it("groups on AccountIdHash and never re-keys revenue off a user", () => {
    const sql = sqlOf();
    expect(sql).toContain("GROUP BY account_id_hash");
    // Joining business_events to spans on UserIdHash would split or duplicate one account's
    // revenue. E2's AccountLinker owns that decision at ingest.
    expect(sql).not.toContain("JOIN");
    expect(sql).not.toContain("otel_spans");
  });

  it("discriminates on ValueType, not a hardcoded source or event name", () => {
    const sql = sqlOf();
    expect(sql).toContain("b.ValueType IN {positiveTypes:Array(String)}");
    expect(sql).toContain("b.ValueType = {refundType:String}");
    expect(sql).not.toContain("EventName");
    expect(sql).not.toContain("'stripe'");
  });

  it("binds the policy's value types, honouring include_mrr", () => {
    expect(accountRevenueSql(DEFAULT_REVENUE_POLICY).params.positiveTypes).toEqual([
      "monetary",
      "mrr",
    ]);
    expect(
      accountRevenueSql({ sources: null, includeMrr: false }).params.positiveTypes,
    ).toEqual(["monetary"]);
  });

  it("narrows by source only when the tenant configured one", () => {
    const open = accountRevenueSql(DEFAULT_REVENUE_POLICY);
    expect(open.sql).not.toContain("lower(b.Source)");
    expect(open.params.revenueSources).toBeUndefined();

    const narrowed = accountRevenueSql({ sources: ["stripe", "csv-upload"], includeMrr: true });
    expect(narrowed.sql).toContain("lower(b.Source) IN {revenueSources:Array(String)}");
    expect(narrowed.params.revenueSources).toEqual(["stripe", "csv-upload"]);
  });

  it("does not filter out accounts with no revenue events", () => {
    // Dropping them would collapse "engagement but no revenue wiring" into "never heard of it".
    expect(sqlOf()).not.toContain("HAVING");
  });
});

describe("accountRevenueReport", () => {
  it("keeps the unattributed bucket separate from the ranked accounts", () => {
    const report = accountRevenueReport([
      row({ account_id_hash: ACCT_A, gross_micro: "300", revenue_events: "1" }),
      row({ account_id_hash: UNATTRIBUTED_ACCOUNT, gross_micro: "999", revenue_events: "9" }),
    ]);
    expect(report.accounts.map((a) => a.accountIdHash)).toEqual([ACCT_A]);
    expect(report.unattributed?.revenueMicroUsd).toBe(999);
    expect(report.windowDays).toBe(30);
  });

  it("recognises the NUL-padded FixedString(64) default as unattributed", () => {
    const report = accountRevenueReport([
      row({ account_id_hash: "\0".repeat(64), gross_micro: "5", revenue_events: "1" }),
    ]);
    expect(report.accounts).toHaveLength(0);
    expect(report.unattributed?.accountIdHash).toBe(UNATTRIBUTED_ACCOUNT);
  });

  it("reports no unattributed bucket when every event carries an account", () => {
    const report = accountRevenueReport([
      row({ account_id_hash: ACCT_A, gross_micro: "1", revenue_events: "1" }),
    ]);
    expect(report.unattributed).toBeNull();
  });

  it("ranks known revenue first and sinks unknown to the bottom", () => {
    // An account we know nothing about must not outrank one we know is losing money.
    const report = accountRevenueReport([
      row({ account_id_hash: ACCT_B, revenue_events: "0" }),
      row({ account_id_hash: ACCT_A, refund_micro: "50", revenue_events: "1" }),
    ]);
    expect(report.accounts.map((a) => a.revenueMicroUsd)).toEqual([-50, null]);
  });
});
