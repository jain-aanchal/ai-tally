// SPDX-License-Identifier: Apache-2.0
// Revenue per account (CTO-196, plan item E3).
//
// Sums `business_events.ValueAmountMicro` grouped by `AccountIdHash` over the same window the cost
// queries use, netting refunds off the total. Pure helpers live here; the ClickHouse round trip is
// `queryAccountRevenue` in lib/clickhouse.ts. Splitting them this way is what lets the null-vs-zero
// rule below be tested without infra, which is the rule the margin column depends on.
//
// Three things this module is deliberate about.
//
// 1. It reuses the E1 revenue policy (lib/revenueSources.ts). `ValueType` is the discriminator,
//    never a hardcoded source or event name, so uploaded revenue (E5) and connector revenue (E2)
//    flow through exactly one policy. A tenant that narrows its revenue sources narrows both.
//
// 2. It reports null, not 0, for an account with no revenue events. Zero reads as "this customer
//    generates no revenue", which is a claim; null reads as "we do not know", which is the truth
//    when nothing has been wired up. An account whose only events are `count` engagement signals
//    is in that second category, not the first, so the row count that decides this counts only
//    money-typed events. Netting a real refund down to exactly 0 stays 0, because that is a
//    measurement rather than an absence.
//
// 3. It groups on the `AccountIdHash` already stamped on the row at ingest. It does NOT join
//    business_events to spans on `UserIdHash` to infer an account. One user belongs to one
//    account, and E2's `AccountLinker` (sdk/python/src/tally/account_identity.py) is where that is
//    decided: a user seen against two accounts is marked ambiguous, the event lands with an empty
//    `AccountIdHash`, and an `AccountConflict` finding goes to the data-quality surface. Re-deriving
//    the mapping here would either duplicate that revenue across accounts or silently disagree with
//    the finding an operator is looking at. Ambiguous revenue arrives in the unattributed bucket,
//    which the page reports rather than hides.

import {
  REFUND_VALUE_TYPE,
  positiveValueTypes,
  revenueSourceFilter,
  type RevenuePolicy,
} from "./revenueSources";

/**
 * The window, as a SQL expression. Calendar aligned and identical to the one every cost query in
 * clickhouse.ts uses (`Timestamp >= toDate(now()) - INTERVAL 29 DAY`), so revenue and cost cover
 * the same period and a margin is a subtraction of two figures measured over the same days.
 * `toDate` truncates to midnight, which is why 29 and not 30 gives a 30 day window.
 */
export const REVENUE_WINDOW_SQL = "toDate(now()) - INTERVAL 29 DAY";

/** Days covered by REVENUE_WINDOW_SQL, for labelling the figure in the UI. */
export const REVENUE_WINDOW_DAYS = 30;

/**
 * The window SQL for an arbitrary day count, so the FilterBar's time range can drive revenue over
 * the same span as cost (CTO-223). `days` is a clamped integer chosen server-side, never user text,
 * so the interpolation is safe. Defaults to the 30-day constant, which reproduces REVENUE_WINDOW_SQL
 * byte-for-byte so an unparameterised caller is unchanged.
 */
export function revenueWindowSql(days: number = REVENUE_WINDOW_DAYS): string {
  return `toDate(now()) - INTERVAL ${Math.max(1, Math.trunc(days)) - 1} DAY`;
}

/** `AccountIdHash` value meaning "no account could be established honestly". */
export const UNATTRIBUTED_ACCOUNT = "";

/** Net revenue for one account over the window. */
export interface AccountRevenue {
  /** The account hash, or `UNATTRIBUTED_ACCOUNT` for the no-account bucket. */
  accountIdHash: string;
  /**
   * Gross minus refunds, in integer micro-USD, or `null` when this account has no money-typed
   * event at all. Never 0 to mean "unknown": see the note at the top of this file.
   */
  revenueMicroUsd: number | null;
  /** Sum of the positive (`monetary`, and `mrr` unless the tenant opted out) amounts. */
  grossMicroUsd: number;
  /** Sum of the `refund` amounts, as a positive number. Subtracted from gross. */
  refundMicroUsd: number;
  /** Money-typed events seen, refunds included. Zero is what makes `revenueMicroUsd` null. */
  revenueEvents: number;
  /** Distinct users who produced them. */
  distinctUsers: number;
}

/** Per-account revenue for a tenant, with the unattributed bucket kept separate. */
export interface AccountRevenueReport {
  /** Accounts carrying at least one money-typed event, highest net revenue first. */
  accounts: AccountRevenue[];
  /**
   * Revenue that could not be tied to an account: never tagged, or tied to a user that E2 marked
   * ambiguous. `null` when there is none. The page shows this rather than dropping it, so a tenant
   * ranking accounts can see how much of the total the ranking leaves out.
   */
  unattributed: AccountRevenue | null;
  /** Window the figures cover, matching the cost side. */
  windowDays: number;
}

/** Raw row shape ClickHouse returns. Int64 sums arrive as strings in JSONEachRow. */
export interface AccountRevenueSqlRow {
  account_id_hash: string;
  gross_micro: string | number | null;
  refund_micro: string | number | null;
  revenue_events: string | number | null;
  distinct_users: string | number | null;
}

function int(v: string | number | null | undefined): number {
  const n = typeof v === "number" ? v : parseInt(v ?? "0", 10);
  return Number.isFinite(n) ? n : 0;
}

/**
 * SQL for the per-account revenue sum, plus the parameters it binds.
 *
 * Every `sumIf` zero-fills its argument with `ifNull(..., 0)`. `ValueAmountMicro` is
 * `Nullable(Int64)`, so a `sumIf` matching no row in a group returns NULL rather than 0, and NULL
 * minus anything is NULL. The E1 agent hit exactly this: on a tenant with no refunds the refund
 * term swallowed the whole subtraction and revenue came back NULL for everyone. The zero-fill is
 * what stops that, and it is why "no revenue events" is decided by `revenue_events` rather than by
 * a NULL sum. Same fix, same shape, as the queryAttribution revenue expression.
 *
 * Amounts stay in micro-USD end to end. There is no divide by 1e6 and re-multiply, so no account
 * total picks up a float rounding error on the way through.
 *
 * There is deliberately no `HAVING revenue_events > 0`. Filtering those groups out would hide the
 * difference between an account we have engagement data for but no revenue wiring, and an account
 * we have never heard of. Both end up blank in the UI, but only one of them is a connector the
 * tenant can go and configure, so the row is worth returning.
 */
export function accountRevenueSql(
  policy: RevenuePolicy,
  windowDays: number = REVENUE_WINDOW_DAYS,
): {
  sql: string;
  params: Record<string, unknown>;
} {
  const positiveTypes = positiveValueTypes(policy);
  const sourceFilter = revenueSourceFilter(policy, "b");
  const moneyTyped =
    `(b.ValueType IN {positiveTypes:Array(String)} ` +
    `OR b.ValueType = {refundType:String})`;

  return {
    sql: `SELECT
        b.AccountIdHash AS account_id_hash,
        sumIf(ifNull(b.ValueAmountMicro, 0), b.ValueType IN {positiveTypes:Array(String)}) AS gross_micro,
        sumIf(abs(ifNull(b.ValueAmountMicro, 0)), b.ValueType = {refundType:String})       AS refund_micro,
        countIf(${moneyTyped})                                                             AS revenue_events,
        uniqExactIf(b.UserIdHash, ${moneyTyped})                                           AS distinct_users
      FROM business_events b
      WHERE b.TenantId = {tenant:String}
        AND b.OccurredAt >= ${revenueWindowSql(windowDays)}
        ${sourceFilter.sql}
      GROUP BY account_id_hash`,
    params: {
      positiveTypes,
      refundType: REFUND_VALUE_TYPE,
      ...sourceFilter.params,
    },
  };
}

/** Map one ClickHouse row onto the typed shape, applying the null-vs-zero rule. */
export function accountRevenueFromRow(row: AccountRevenueSqlRow): AccountRevenue {
  const gross = int(row.gross_micro);
  const refund = int(row.refund_micro);
  const revenueEvents = int(row.revenue_events);
  return {
    accountIdHash: row.account_id_hash ?? UNATTRIBUTED_ACCOUNT,
    // No money-typed event means we have not been told this account's revenue. An account whose
    // only events are `count` signals lands here, and must read as unknown rather than as zero.
    revenueMicroUsd: revenueEvents > 0 ? gross - refund : null,
    grossMicroUsd: gross,
    refundMicroUsd: refund,
    revenueEvents,
    distinctUsers: int(row.distinct_users),
  };
}

/** Build the report from raw rows, splitting the unattributed bucket out and ranking the rest. */
export function accountRevenueReport(
  rows: AccountRevenueSqlRow[],
  windowDays: number = REVENUE_WINDOW_DAYS,
): AccountRevenueReport {
  let unattributed: AccountRevenue | null = null;
  const accounts: AccountRevenue[] = [];

  for (const raw of rows) {
    const rec = accountRevenueFromRow(raw);
    // FixedString(64) pads with NUL bytes, so the default '' can arrive as a run of them.
    if (rec.accountIdHash.replace(/\0/g, "") === UNATTRIBUTED_ACCOUNT) {
      unattributed = { ...rec, accountIdHash: UNATTRIBUTED_ACCOUNT };
    } else {
      accounts.push(rec);
    }
  }

  // Known revenue ranks first, highest to lowest. Unknown sinks to the bottom rather than sorting
  // as 0, which would otherwise rank an account we know nothing about above one we know is negative.
  accounts.sort((a, b) => {
    const aKnown = a.revenueMicroUsd !== null;
    const bKnown = b.revenueMicroUsd !== null;
    if (aKnown !== bKnown) return aKnown ? -1 : 1;
    if (aKnown && bKnown && a.revenueMicroUsd !== b.revenueMicroUsd) {
      return (b.revenueMicroUsd as number) - (a.revenueMicroUsd as number);
    }
    return a.accountIdHash.localeCompare(b.accountIdHash);
  });
  return { accounts, unattributed, windowDays };
}

/**
 * Revenue for one account, for the margin column.
 *
 * Returns `null` for an account the report has no row for, which is the same answer as an account
 * with no money-typed events: we do not know. The caller renders a blank either way, and must not
 * substitute 0.
 */
export function revenueForAccount(
  report: AccountRevenueReport | null,
  accountIdHash: string,
): number | null {
  if (!report) return null;
  const hit = report.accounts.find((a) => a.accountIdHash === accountIdHash);
  return hit ? hit.revenueMicroUsd : null;
}
