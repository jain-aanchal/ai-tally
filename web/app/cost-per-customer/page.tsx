// SPDX-License-Identifier: Apache-2.0
// Cost per customer (CTO-188, plan D2).
//
// Direct spend broken down by the tenant's own accounts, ranked by cost. Two things make this page
// different from the rest of the dashboard, and both are deliberate:
//
// 1. NO SAMPLE DATA. Every other tab ships a canned fixture because its page landed before the
//    telemetry did; lib/accounts.ts ships none on purpose. A plausible list of fake customer names
//    is the single most misleading thing this page could show, so when there is nothing to report
//    it says so.
//
// 2. THE UNATTRIBUTED SHARE LEADS. If 60 percent of spend carries no account, ranking the other 40
//    percent as though it were the whole picture is a lie of omission. The headline states the
//    share before the table, every time, including when it is 100 percent, which is exactly what a
//    tenant that has not instrumented `account_id` yet will see.
//
// 3. NOTHING IS SILENTLY ESTIMATED. Compute and egress carry no account and are roughly half of
//    spend, so they are ALLOCATED per account (CTO-193) rather than dropped. Allocated cost is its
//    own column, never folded into one number, and the rule that produced it is named on screen.
//    CTO-189's excluded-cost banner is gone: there is no longer an excluded half to warn about,
//    and a stale banner beside allocated columns would be worse than no banner at all.
//
// 4. EMPTY IS EXPLAINED, NOT SHRUGGED AT (CTO-191). Because nothing emits `account_id` yet, the
//    common case on release is a tenant with no accounts at all, seeing this tab for the first
//    time. `accountsView` sorts a successful query into three readings that need different copy:
//    no accounts (explain the page, then how to switch it on), a partial ranking (show it, and
//    offer the snippet that finishes it), and a normal one. An unreachable store is deliberately
//    not one of them, because answering our own outage with an onboarding pitch would blame the
//    reader for it.
//
// Reads the query directly rather than through /api, matching how the page-level gateway reads on
// /connectors work: there is no client-side refetch here and no second consumer of the payload, so
// a route handler would only add a hop.

import { Card } from "@/components/Card";
import { FilterBar, type FilterOption } from "@/components/FilterBar";
import { Blank, Money } from "@/components/HonestValue";
import { PageHeader } from "@/components/PageHeader";
import { SummaryTile, TileGrid } from "@/components/SummaryTile";
import {
  ALLOCATION_RULE_DESCRIPTIONS,
  ALLOCATION_RULE_LABELS,
  accountDisplayName,
  accountsView,
  allocateAccountCosts,
  allocatedRowsTotal,
  attributedSpend,
  formatShare,
  unattributedShare,
  type AccountCosts,
  type AllocatedAccountCosts,
  type ExcludedInfraCost,
} from "@/lib/accounts";
import { queryAccountLabels } from "@/lib/accountLabels";
import { type AccountRevenueReport } from "@/lib/accountRevenue";
import { queryAllocationRule, type AllocationRuleSetting } from "@/lib/allocationConfig";
import {
  queryAccountCosts,
  queryAccountFeatureTags,
  queryAccountRevenue,
  queryExcludedInfraCost,
} from "@/lib/clickhouse";
import { parseFilters, rangeDays } from "@/lib/filters";
import { AccountTable } from "./AccountTable";
import { HowToTagDetails, OnboardingEmptyState } from "./Onboarding";

export const dynamic = "force-dynamic";

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function CostPerCustomerPage({ searchParams }: PageProps) {
  const params = await searchParams;
  // Read the same URL query the FilterBar writes (CTO-223): the time range drives the window every
  // read below runs under, and the feature / account multi-selects narrow the slice. `?tag=`/`?scope=`
  // and any other unrelated param are preserved by the shared filter serialization.
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (typeof v === "string") sp.set(k, v);
    else if (Array.isArray(v) && v[0] !== undefined) sp.set(k, v[0]);
  }
  const filterState = parseFilters(sp);
  const windowDays = rangeDays(filterState.range);
  const features = filterState.filters.feature;
  const accounts = filterState.filters.account;
  const hasDimFilter = features.length > 0 || accounts.length > 0;

  const [costs, labels, revenue, excluded, allocationSetting, featureTags] = await Promise.all([
    queryAccountCosts({ windowDays, features, accounts }),
    queryAccountLabels(),
    queryAccountRevenue(windowDays),
    queryExcludedInfraCost({ windowDays, features, accounts }),
    queryAllocationRule(),
    queryAccountFeatureTags(windowDays),
  ]);

  // Filter options for the bar. Features come from the window's directly attributable spend; accounts
  // are the rows we already have, shown by label where one exists so the dropdown is not a wall of
  // hashes. A dimension with no options renders no control (see FilterBar), so a fresh tenant with no
  // features or accounts simply sees the time range.
  const labelMap = labels ?? new Map<string, string>();
  const featureOptions: FilterOption[] = (featureTags ?? []).map((f) => ({ value: f }));
  const accountOptions: FilterOption[] = (costs?.accounts ?? []).map((r) => ({
    value: r.accountIdHash,
    label: accountDisplayName(r.accountIdHash, labelMap.get(r.accountIdHash)),
  }));

  const toolbar = (
    <FilterBar
      hideGroupBy
      options={{ feature: featureOptions, account: accountOptions }}
    />
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cost per User"
        subtitle={
          costs
            ? `Direct spend by account plus each account's allocated share of compute and egress, over the last ${costs.windowDays} days.`
            : "Direct spend by account plus each account's allocated share of compute and egress."
        }
        toolbar={toolbar}
      />

      <p className="text-sm text-muted">
        Cost per account: measured direct spend (LLM, tools, vector, embeddings), plus each
        account&apos;s estimated share of shared compute and egress. Direct is measured, allocated is
        an estimate, and the two are shown separately.
      </p>

      {costs === null ? (
        <Unreachable />
      ) : (
        <Report
          costs={costs}
          labels={labels}
          revenue={revenue}
          excluded={excluded}
          allocationSetting={allocationSetting}
          filtered={hasDimFilter}
        />
      )}
    </div>
  );
}

function Report({
  costs,
  labels,
  revenue,
  excluded,
  allocationSetting,
  filtered,
}: {
  costs: AccountCosts;
  labels: Map<string, string> | null;
  /** `null` when the revenue read failed: a different statement from "no revenue is wired". */
  revenue: AccountRevenueReport | null;
  excluded: ExcludedInfraCost | null;
  allocationSetting: AllocationRuleSetting;
  /**
   * A feature or account filter is narrowing the slice (CTO-223). The whole-tenant reconciliation
   * line below claims the rows sum to what `/cost` reports for the window, which is only true of the
   * unfiltered view, so it is suppressed here rather than left to make a false claim about a slice.
   */
  filtered: boolean;
}) {
  const share = unattributedShare(costs);
  const attributed = attributedSpend(costs);
  // `null` when the compute and egress total could not be read. Nothing is allocated in that case
  // and the page says so; it does not print direct cost as though it were total cost.
  const allocated = allocateAccountCosts(costs, excluded, allocationSetting.rule);
  const view = accountsView(costs);
  // Hash to net revenue, straight from the report. Accounts the report has no row for are simply
  // absent, which the table reads as unknown; an account present with `revenueMicroUsd: null` says
  // the same thing, and an account with `0` says something different and keeps its zero.
  const revenueByAccount = Object.fromEntries(
    (revenue?.accounts ?? []).map((a) => [a.accountIdHash, a.revenueMicroUsd]),
  );

  const windowHint = `last ${costs.windowDays} days`;

  return (
    <div className="space-y-6">
      <TileGrid>
        <SummaryTile
          label="Direct spend (measured)"
          micro={costs.totalDirectMicroUsd}
          hint={windowHint}
        />
        <SummaryTile
          label="Allocated infra (estimated)"
          micro={allocated === null ? null : allocated.allocatedTotalMicroUsd}
          reason="the compute and egress total could not be read, so nothing could be allocated"
          hint={windowHint}
        />
        <SummaryTile
          label="Total cost"
          micro={allocated === null ? null : allocated.tenantTotalMicroUsd}
          reason="total cost needs the compute and egress figure, which could not be read"
          hint={windowHint}
        />
        {/* The unattributed share is a percentage, not money, so it keeps its own honest-blank tile
            rather than passing through Money. The valve returns `null` only when the tenant recorded
            no direct spend at all, where "0% unattributed" would claim perfect coverage of nothing. */}
        <ShareTile
          share={share}
          reason={`no directly attributable spend recorded in the ${windowHint}, so there is no share to report`}
          hint={windowHint}
        />
      </TileGrid>

      {filtered ? (
        <p className="rounded-lg border border-accent/30 bg-accent/5 px-3 py-2 text-xs text-muted">
          Filtered view: only the selected features and accounts are counted, so every total here is
          a slice of the tenant&apos;s spend rather than the whole bill. The whole-tenant
          reconciliation against the Cost tab is hidden until you clear the filter.
        </p>
      ) : null}

      <p className="text-xs text-muted">
        Attributed direct spend is <Money micro={attributed} /> of{" "}
        <Money micro={costs.totalDirectMicroUsd} /> across{" "}
        {`${costs.accounts.length.toLocaleString()} ${
          costs.accounts.length === 1 ? "account" : "accounts"
        }.`}
      </p>

      {view !== "attributed" && share !== null ? (
        <UnattributedNotice costs={costs} share={share} />
      ) : null}

      <AllocationNotice allocated={allocated} setting={allocationSetting} costs={costs} />

      {/* No accounts at all is not an empty table, it is a reader who has never seen this page.
          The onboarding state replaces the table entirely (CTO-191); the honest headline figures
          and the unattributed notice above it still render, so the explainer never stands in for
          a number the page owes. */}
      {view === "onboarding" ? (
        <OnboardingEmptyState windowDays={costs.windowDays} />
      ) : (
        <Card title="Accounts by gross margin">
          <AccountTable
            rows={allocated ? allocated.accounts : costs.accounts}
            labels={labels ? Object.fromEntries(labels) : {}}
            labelsUnavailable={labels === null}
            revenue={revenueByAccount}
            revenueUnavailable={revenue === null}
            windowDays={costs.windowDays}
            allocationRule={allocated ? allocated.effectiveRule : null}
          />
          {allocated ? <UnattributedRow allocated={allocated} /> : null}
          {labels === null ? (
            <p className="mt-3 text-xs text-warn">
              Account labels could not be read from the gateway, so every account shows as a hash.
              An unlabelled row here is not proof that no label is set.
            </p>
          ) : null}
          {/* Partial instrumentation: the ranking above is real but covers a minority of spend, so
              the snippet that finishes the job sits one click away rather than repeating the whole
              onboarding explainer under a table the reader can already see. */}
          {view === "partial" ? (
            <div className="mt-4">
              <HowToTagDetails />
            </div>
          ) : null}
        </Card>
      )}

      {/* The reconciliation line sums the RENDERED rows, so it only prints where rows were
          rendered. In the onboarding state there is no table to reconcile, and under a dimension
          filter it would claim a slice equals the whole-tenant bill, so it is suppressed there too. */}
      {allocated && view !== "onboarding" && !filtered ? (
        <Reconciliation allocated={allocated} />
      ) : null}
    </div>
  );
}

/**
 * The honest headline when most spend carries no account.
 *
 * Written to read as a deliberate statement rather than a broken page, because on a tenant that has
 * not instrumented `account_id` this is the normal state and it will be the first thing anyone
 * sees. It names the number and says what the table below it does and does not cover.
 *
 * It stops there on purpose. What follows it differs by state (CTO-191): with no accounts at all
 * the whole onboarding explainer takes over from the table, and with a partial ranking the snippet
 * sits in a disclosure under it. Putting the instructions in here as well would show them twice.
 */
function UnattributedNotice({ costs, share }: { costs: AccountCosts; share: number }) {
  const complete = costs.accounts.length === 0;
  return (
    <div className="rounded-xl border border-warn/40 bg-warn/5 p-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="rounded bg-warn/20 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-warn">
          Mostly unattributed
        </span>
        <span className="text-sm">
          <span className="font-semibold">{formatShare(share)}</span> of direct spend (
          <Money micro={costs.unattributed.directCostMicroUsd} /> of{" "}
          <Money micro={costs.totalDirectMicroUsd} />) carries no account id.
        </span>
      </div>
      <p className="mt-2 text-sm text-muted">
        {complete
          ? `Nothing in the last ${costs.windowDays} days is tagged with an account, so there is no per-customer breakdown to rank yet. This is what the page looks like before an account id is emitted, not an error. What the page does once one is, and how to emit it, is below.`
          : `The accounts below cover the remaining spend only. Ranking them as though they were the whole picture would misstate what each customer costs, so read them as a partial view until more spans carry an account id. The snippet under the table tags the rest.`}
      </p>
    </div>
  );
}

/**
 * The rule that produced the allocated column, named (CTO-193, plan C2).
 *
 * This replaces CTO-189's excluded-cost banner, which said how much the table left out. Nothing is
 * left out now, so the honest thing to state is no longer a size but an ASSUMPTION: roughly half of
 * every total on this page was produced by a rule rather than measured, and a reader who cannot see
 * which rule it was has no way to judge or argue with the number. An allocation rule nobody can see
 * is an invisible assumption, which is the same failure in a different disguise.
 *
 * Four states, each saying something different:
 *   - allocated, with the rule and where it came from (tenant choice, product default, or the
 *     default applied because the config could not be read, which is NOT the same claim)
 *   - allocated by a fallback rule because pro rata had no denominator: the engine says so and so
 *     does this, because the rule in force is not the rule that was asked for
 *   - nothing to allocate: no compute or egress in the window at all
 *   - unreadable: the compute and egress total could not be read, so nothing was allocated and the
 *     totals on this page are a floor rather than a total
 */
function AllocationNotice({
  allocated,
  setting,
  costs,
}: {
  allocated: AllocatedAccountCosts | null;
  setting: AllocationRuleSetting;
  costs: AccountCosts;
}) {
  if (allocated === null) {
    return (
      <div className="rounded-xl border border-warn/40 bg-warn/5 p-4 text-sm">
        <p>
          Compute and egress could not be read for the last {costs.windowDays} days, so nothing has
          been allocated and every figure here is direct cost only:{" "}
          <Blank reason="the compute and egress total could not be read from the telemetry store, so no allocated share could be computed" />
          . On a typical tenant that infrastructure is close to half the bill, so read the accounts
          below as a floor on true cost, not a total.
        </p>
      </div>
    );
  }

  if (allocated.sharedMicroUsd <= 0) {
    return (
      <div className="rounded-xl border border-edge bg-panel p-4 text-sm text-muted">
        <p>
          No compute or egress was recorded in the last {allocated.windowDays} days, so there is
          nothing to allocate and every total below is measured spend. Allocated cost stays at zero
          until a cloud billing connector lands infrastructure spend for this tenant.
        </p>
      </div>
    );
  }

  const rule = allocated.effectiveRule;
  const fellBack = allocated.effectiveRule !== allocated.rule;
  const unattributedAllocated = allocated.unattributed.allocatedMicroUsd;
  const bucketShare =
    allocated.allocatedTotalMicroUsd > 0
      ? unattributedAllocated / allocated.allocatedTotalMicroUsd
      : null;

  return (
    <div className="rounded-xl border border-edge bg-panel p-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-accent">
          Allocated
        </span>
        <span className="text-sm">
          <Money micro={allocated.sharedMicroUsd} className="font-semibold" /> of compute and egress
          is split across accounts{" "}
          <span className="font-semibold">{ALLOCATION_RULE_LABELS[rule]}</span>{" "}
          <RuleSource setting={setting} fellBack={fellBack} />.
        </span>
      </div>
      <p className="mt-2 text-sm text-muted">
        {/* The descriptions read as a clause so a column tooltip can prefix them; this is the one
            place they open a sentence, so the first letter is raised here rather than duplicating
            the text in two cases. */}
        {ALLOCATION_RULE_DESCRIPTIONS[rule].charAt(0).toUpperCase()}
        {ALLOCATION_RULE_DESCRIPTIONS[rule].slice(1)}. Compute and egress arrive as tenant-level
        totals with no account attached, so this is an estimate, shown in its own column.
      </p>
      <p className="mt-2 text-sm text-muted">
        {/* The unattributed-bucket decision, stated where the reader meets its consequences. On a
            tenant that has barely instrumented account_id, this paragraph is the difference between
            a page that reads as broken and one that reads as true. */}
        Untagged traffic joins the split as one participant, carrying{" "}
        <Money micro={unattributedAllocated} />
        {bucketShare === null ? "" : ` (${formatShare(bucketShare)})`} of the shared infrastructure
        it caused. Leaving it out would overstate every tagged account.
      </p>
    </div>
  );
}

/** Where the rule in force came from. Three different claims, so three different sentences. */
function RuleSource({
  setting,
  fellBack,
}: {
  setting: AllocationRuleSetting;
  fellBack: boolean;
}) {
  if (fellBack) {
    // The engine could not apply the configured rule: pro rata needs a denominator and every
    // account had zero direct spend. Naming the configured rule here would describe an arithmetic
    // that did not happen.
    return (
      <span title={`Configured rule: ${ALLOCATION_RULE_LABELS[setting.rule]}`}>
        {`(fallback: ${ALLOCATION_RULE_LABELS[setting.rule]} had no direct spend to divide by)`}
      </span>
    );
  }
  if (setting.source === "tenant") {
    return (
      <span title={setting.updatedBy ? `Last changed by ${setting.updatedBy}` : undefined}>
        {`(this tenant's configured rule${
          setting.updatedAt ? `, set ${setting.updatedAt.slice(0, 10)}` : ""
        })`}
      </span>
    );
  }
  if (setting.source === "default") {
    return <span>(the product default; no rule configured for this tenant)</span>;
  }
  // Applied the default WITHOUT being able to check the tenant's own choice. Saying "the default"
  // flat would be a claim about their configuration that we did not verify.
  return (
    <span className="text-warn">
      (the product default, applied because the allocation config could not be read)
    </span>
  );
}

/**
 * The untagged bucket, below the table rather than inside it.
 *
 * It is not a customer and must never be ranked as one, but on the current data it carries most of
 * the money, so leaving it out of the page entirely would make the table appear to lose the bill.
 * Shown as its own panel with the same three figures, so the reconciliation line below adds up in
 * front of the reader.
 */
function UnattributedRow({ allocated }: { allocated: AllocatedAccountCosts }) {
  const row = allocated.unattributed;
  return (
    <div className="mt-4 rounded-lg border border-dashed border-edge bg-ink/20 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 text-sm">
        <span className="font-medium">
          Untagged traffic{" "}
          <span className="text-xs font-normal text-muted">
            (not a customer: spans that carry no account id)
          </span>
        </span>
        <span className="flex flex-wrap gap-x-6 tabular-nums">
          <span>
            <span className="text-xs uppercase tracking-wide text-muted">Direct </span>
            <Money micro={row.directCostMicroUsd} />
          </span>
          <span className="text-muted">
            <span className="text-xs uppercase tracking-wide">Allocated </span>
            <Money micro={row.allocatedMicroUsd} />
          </span>
          <span className="font-medium">
            <span className="text-xs uppercase tracking-wide text-muted">Total </span>
            <Money micro={row.totalMicroUsd} />
          </span>
        </span>
      </div>
    </div>
  );
}

/**
 * The reconciliation line: this page against `/cost`.
 *
 * The acceptance test of the whole feature, printed rather than merely unit-tested. If the rows do
 * not add up to the tenant's bill, the tab contradicts `/cost` and loses trust the first time
 * anyone sums a column, so the sum is put on screen where a reader can check it themselves.
 *
 * The total is summed from the rendered rows, not read back off the allocation result, so the line
 * is an actual check on what was displayed. `allocateShared` guarantees the equality, which makes
 * the mismatch branch dead code on every input the engine accepts. It is kept anyway: this claim is
 * load-bearing enough that silently printing a wrong sum would be worse than admitting a bug.
 */
function Reconciliation({ allocated }: { allocated: AllocatedAccountCosts }) {
  const summed = allocatedRowsTotal(allocated);
  const reconciles = summed === allocated.tenantTotalMicroUsd;
  return (
    <p className="text-xs text-muted">
      Direct <Money micro={allocated.directTotalMicroUsd} /> plus allocated{" "}
      <Money micro={allocated.allocatedTotalMicroUsd} /> is{" "}
      <Money micro={allocated.tenantTotalMicroUsd} className="font-medium" />, the tenant total for
      the same {allocated.windowDays} days on the Cost tab. Every row above, untagged traffic
      included, adds up to exactly that figure
      {reconciles ? (
        "."
      ) : (
        <span className="text-warn">
          {", except they do not: the rows sum to "}
          <Money micro={summed} />, which is a bug in this page rather than a fact about your bill.
        </span>
      )}
    </p>
  );
}

/**
 * ClickHouse unreachable. No fixture stands in: an invented customer list is worse than an empty
 * page, and this is the one surface where the fallback would be indistinguishable from real data.
 */
function Unreachable() {
  return (
    <Card title="Accounts by direct cost">
      <p className="text-sm text-muted">
        The telemetry store could not be reached, so there is nothing to report. This page has no
        sample data by design: a made-up list of customer accounts would be impossible to tell from
        a real one.
      </p>
      <div className="mt-3">
        <Blank reason="the telemetry store is unreachable, so no per-account cost could be read" />
      </div>
    </Card>
  );
}

/**
 * The unattributed-share tile, matching the SummaryTile shape but rendering a percentage rather than
 * money. `share === null` is the honesty valve (no direct spend at all), so it renders the same
 * explained blank the money tiles do rather than a fabricated "0%".
 */
function ShareTile({
  share,
  reason,
  hint,
}: {
  share: number | null;
  reason: string;
  hint: string;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-edge bg-panel p-4">
      <span className="text-xs font-medium uppercase tracking-wide text-muted">
        Direct spend with no account
      </span>
      <span className="text-2xl font-semibold tabular-nums">
        {share === null ? <Blank reason={reason} /> : formatShare(share)}
      </span>
      <span className="text-xs text-muted">{hint}</span>
    </div>
  );
}
