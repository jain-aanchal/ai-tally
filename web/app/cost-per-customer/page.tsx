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
// Reads the query directly rather than through /api, matching how the page-level gateway reads on
// /connectors work: there is no client-side refetch here and no second consumer of the payload, so
// a route handler would only add a hop.

import { Card } from "@/components/Card";
import { Blank, Money } from "@/components/HonestValue";
import {
  attributedSpend,
  formatShare,
  unattributedShare,
  type AccountCosts,
} from "@/lib/accounts";
import { queryAccountLabels } from "@/lib/accountLabels";
import { queryAccountCosts } from "@/lib/clickhouse";
import { AccountTable } from "./AccountTable";

export const dynamic = "force-dynamic";

/** Above this share, the unattributed bucket is the story rather than a footnote. */
const MAJORITY_UNATTRIBUTED = 0.5;

export default async function CostPerCustomerPage() {
  const [costs, labels] = await Promise.all([queryAccountCosts(), queryAccountLabels()]);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Cost per customer</h1>
        {costs ? (
          <span className="text-sm text-muted">
            Direct spend, last {costs.windowDays} days
          </span>
        ) : null}
      </div>

      <p className="max-w-prose text-sm text-muted">
        Directly attributable spend (LLM, tools, vector, embeddings) grouped by the account each span
        was tagged with. Compute and egress are excluded: no span carries an account for them, so
        splitting them per customer would mean inventing an allocation rule.
      </p>

      {costs === null ? <Unreachable /> : <Report costs={costs} labels={labels} />}
    </div>
  );
}

function Report({
  costs,
  labels,
}: {
  costs: AccountCosts;
  labels: Map<string, string> | null;
}) {
  const share = unattributedShare(costs);
  const attributed = attributedSpend(costs);

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="Attributed spend">
          <Money micro={attributed} />
        </Stat>
        <Stat label="Accounts">
          <span className="tabular-nums">{costs.accounts.length.toLocaleString()}</span>
        </Stat>
        <Stat label="Spend with no account">
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

      {share !== null && share >= MAJORITY_UNATTRIBUTED ? (
        <UnattributedNotice costs={costs} share={share} />
      ) : null}

      <Card title="Accounts by direct cost">
        <AccountTable
          rows={costs.accounts}
          labels={labels ? Object.fromEntries(labels) : {}}
          labelsUnavailable={labels === null}
          windowDays={costs.windowDays}
        />
        {labels === null ? (
          <p className="mt-3 text-xs text-warn">
            Account labels could not be read from the gateway, so every account shows as a hash. An
            unlabelled row here is not proof that no label is set.
          </p>
        ) : null}
      </Card>
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
