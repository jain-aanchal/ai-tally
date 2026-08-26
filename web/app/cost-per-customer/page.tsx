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
// Reads the query directly rather than through /api, matching how the page-level gateway reads on
// /connectors work: there is no client-side refetch here and no second consumer of the payload, so
// a route handler would only add a hop.

import { Card } from "@/components/Card";
import { Blank, Money } from "@/components/HonestValue";
import {
  ALLOCATION_RULE_DESCRIPTIONS,
  ALLOCATION_RULE_LABELS,
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
import { queryAllocationRule, type AllocationRuleSetting } from "@/lib/allocationConfig";
import { queryAccountCosts, queryExcludedInfraCost } from "@/lib/clickhouse";
import { AccountTable } from "./AccountTable";

export const dynamic = "force-dynamic";

/** Above this share, the unattributed bucket is the story rather than a footnote. */
const MAJORITY_UNATTRIBUTED = 0.5;

export default async function CostPerCustomerPage() {
  const [costs, labels, excluded, allocationSetting] = await Promise.all([
    queryAccountCosts(),
    queryAccountLabels(),
    queryExcludedInfraCost(),
    queryAllocationRule(),
  ]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Cost per customer</h1>
        {costs ? (
          <span className="text-sm text-muted">Last {costs.windowDays} days</span>
        ) : null}
      </div>

      <p className="max-w-prose text-sm text-muted">
        Directly attributable spend (LLM, tools, vector, embeddings) grouped by the account each span
        was tagged with, plus each account&apos;s allocated share of compute and egress, which carry
        no account of their own. Direct cost is measured. Allocated cost is an estimate, and the two
        are never added into a single number without saying so.
      </p>

      {costs === null ? (
        <Unreachable />
      ) : (
        <Report
          costs={costs}
          labels={labels}
          excluded={excluded}
          allocationSetting={allocationSetting}
        />
      )}
    </div>
  );
}

function Report({
  costs,
  labels,
  excluded,
  allocationSetting,
}: {
  costs: AccountCosts;
  labels: Map<string, string> | null;
  excluded: ExcludedInfraCost | null;
  allocationSetting: AllocationRuleSetting;
}) {
  const share = unattributedShare(costs);
  const attributed = attributedSpend(costs);
  // `null` when the compute and egress total could not be read. Nothing is allocated in that case
  // and the page says so; it does not print direct cost as though it were total cost.
  const allocated = allocateAccountCosts(costs, excluded, allocationSetting.rule);

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Direct spend (measured)">
          <Money micro={costs.totalDirectMicroUsd} />
        </Stat>
        <Stat label="Allocated infra (estimated)">
          {allocated === null ? (
            <Blank reason="the compute and egress total could not be read, so nothing could be allocated" />
          ) : (
            <Money micro={allocated.allocatedTotalMicroUsd} />
          )}
        </Stat>
        <Stat label="Total cost">
          {allocated === null ? (
            <Blank reason="total cost needs the compute and egress figure, which could not be read" />
          ) : (
            <Money micro={allocated.tenantTotalMicroUsd} />
          )}
        </Stat>
        <Stat label="Direct spend with no account">
          {/* The valve. `null` only when the tenant recorded no direct spend at all, where "0%
              unattributed" would claim perfect coverage of nothing. */}
          {share === null ? (
            <Blank
              reason={`no directly attributable spend recorded in the last ${costs.windowDays} days, so there is no share to report`}
            />
          ) : (
            formatShare(share)
          )}
        </Stat>
      </div>

      <p className="max-w-prose text-xs text-muted">
        Attributed direct spend is <Money micro={attributed} /> of{" "}
        <Money micro={costs.totalDirectMicroUsd} /> across{" "}
        {`${costs.accounts.length.toLocaleString()} ${
          costs.accounts.length === 1 ? "account" : "accounts"
        }.`}
      </p>

      {share !== null && share >= MAJORITY_UNATTRIBUTED ? (
        <UnattributedNotice costs={costs} share={share} />
      ) : null}

      <AllocationNotice allocated={allocated} setting={allocationSetting} costs={costs} />

      <Card title={allocated ? "Accounts by total cost" : "Accounts by direct cost"}>
        <AccountTable
          rows={allocated ? allocated.accounts : costs.accounts}
          labels={labels ? Object.fromEntries(labels) : {}}
          labelsUnavailable={labels === null}
          windowDays={costs.windowDays}
          allocationRule={allocated ? allocated.effectiveRule : null}
        />
        {allocated ? <UnattributedRow allocated={allocated} /> : null}
        {labels === null ? (
          <p className="mt-3 text-xs text-warn">
            Account labels could not be read from the gateway, so every account shows as a hash. An
            unlabelled row here is not proof that no label is set.
          </p>
        ) : null}
      </Card>

      {allocated ? <Reconciliation allocated={allocated} /> : null}
    </div>
  );
}

/**
 * The honest headline when most spend carries no account.
 *
 * Written to read as a deliberate statement rather than a broken page, because on a tenant that has
 * not instrumented `account_id` this is the normal state and it will be the first thing anyone
 * sees. It names the number, says what the table below it does and does not cover, and stops. The
 * "here is how to switch it on" onboarding state is CTO-191.
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
      <p className="mt-2 max-w-prose text-sm text-muted">
        {complete
          ? `Nothing in the last ${costs.windowDays} days is tagged with an account, so there is no per-customer breakdown to rank yet. This is what the page looks like before an account id is emitted, not an error.`
          : `The accounts below cover the remaining spend only. Ranking them as though they were the whole picture would misstate what each customer costs, so read them as a partial view until more spans carry an account id.`}
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
        <p className="max-w-prose">
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
        <p className="max-w-prose">
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
      <p className="mt-2 max-w-prose text-sm text-muted">
        {/* The descriptions read as a clause so a column tooltip can prefix them; this is the one
            place they open a sentence, so the first letter is raised here rather than duplicating
            the text in two cases. */}
        {ALLOCATION_RULE_DESCRIPTIONS[rule].charAt(0).toUpperCase()}
        {ALLOCATION_RULE_DESCRIPTIONS[rule].slice(1)}. Those spans arrive from the cloud billing
        connectors as
        tenant-level daily totals with no account on them, so an allocated figure is an estimate and
        is shown in its own column rather than folded into direct cost.
      </p>
      <p className="mt-2 max-w-prose text-sm text-muted">
        {/* The unattributed-bucket decision, stated where the reader meets its consequences. On a
            tenant that has barely instrumented account_id, this paragraph is the difference between
            a page that reads as broken and one that reads as true. */}
        Untagged traffic takes part in the split as one participant, so it carries{" "}
        <Money micro={unattributedAllocated} />
        {bucketShare === null ? "" : ` (${formatShare(bucketShare)})`} of the infrastructure it
        caused. Excluding it would divide the whole infrastructure bill
        across only the accounts that happen to be tagged today, which would overstate every one of
        them and make each newly tagged account look like it cut the others&apos; costs.
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
    <p className="max-w-prose text-xs text-muted">
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
      <p className="max-w-prose text-sm text-muted">
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

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-edge bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{children}</div>
    </div>
  );
}
