// SPDX-License-Identifier: Apache-2.0
// One account (CTO-190, plan D4).
//
// Reached by clicking an account on /cost-per-customer. Four questions, in the order someone asks
// them after seeing a row is expensive: what is the money made of (layer split), what is it being
// spent on (top features), is it growing (30-day trend), and which individual runs are the heavy
// ones (heaviest runs). The parent tab's posture carries over unchanged: this page only ever
// reports DIRECT spend, and where it cannot stand behind a figure it prints an explained blank
// rather than a plausible number.
//
// FOUR ENDINGS, kept apart on purpose. A valid hash with rows renders the report. A valid hash
// with no rows says "no spend recorded for this account" and is not a 404: the lookup endpoint
// (B6) hands out well-formed hashes for account ids nobody has ever emitted, so a typo and an idle
// customer arrive here identically and "not found" would misdescribe both. A segment that is not a
// hash at all is the only genuinely malformed case, and it says so. An unreachable store is the
// fourth, and is never folded into the second: telling a reader "no spend recorded" while
// ClickHouse is down is the page asserting something it cannot know.
//
// Reads the queries directly rather than through /api, matching the parent tab: no client-side
// refetch, no second consumer of the payload, so a route handler would only add a hop.

import Link from "next/link";

import { Card } from "@/components/Card";
import { Blank, Money, Pct } from "@/components/HonestValue";
import {
  DIRECT_LAYERS,
  type AccountDetail,
  type DirectLayer,
  accountDisplayName,
  costPerUser,
  isAccountHash,
  layerShare,
  trendTotal,
} from "@/lib/accounts";
import { queryAccountLabels } from "@/lib/accountLabels";
import { queryAccountDetailResult } from "@/lib/clickhouse";
import { LAYERS, LAYER_LABEL, type Layer } from "@/lib/cost";
import { CopyHashButton } from "../AccountIdentity";
import { FeatureTable, RunTable } from "./DetailTables";
import { TrendChart } from "./TrendChart";

export const dynamic = "force-dynamic";

/** Layers this page can never attribute to one account. Kept as the complement of DIRECT_LAYERS. */
const EXCLUDED_LAYERS: readonly Layer[] = LAYERS.filter(
  (l) => !(DIRECT_LAYERS as readonly string[]).includes(l),
);

export default async function AccountDetailPage({
  params,
}: {
  params: Promise<{ account: string }>;
}) {
  const { account } = await params;
  // The segment is user-editable, so its shape is checked before it reaches a query. This is a
  // shape test only: a well-formed hash matching no rows is handled below as an ordinary answer.
  const accountIdHash = decodeURIComponent(account);
  if (!isAccountHash(accountIdHash)) {
    return (
      <Frame heading="Unknown account">
        <Card title="Account">
          <p className="max-w-prose text-sm text-muted">
            That address is not an account id. An account is identified here by the 64-character hex
            hash shown beside every row on the accounts table, which is also what its copy control
            puts on your clipboard. Pick an account from the table rather than typing an address.
          </p>
        </Card>
      </Frame>
    );
  }

  const [result, labels] = await Promise.all([
    queryAccountDetailResult(accountIdHash),
    queryAccountLabels(),
  ]);
  const label = labels?.get(accountIdHash);
  const heading = accountDisplayName(accountIdHash, label);

  if (result.state === "unreachable") {
    return (
      <Frame heading={heading} accountIdHash={accountIdHash} labelled={label !== undefined}>
        <Card title="Account">
          <p className="max-w-prose text-sm text-muted">
            The telemetry store could not be reached, so nothing about this account could be read.
            This is deliberately not reported as &ldquo;no spend&rdquo;: an unreachable store and an
            idle customer are different facts and only one of them is about the customer.
          </p>
          <div className="mt-3">
            <Blank reason="the telemetry store is unreachable, so no cost could be read for this account" />
          </div>
        </Card>
      </Frame>
    );
  }

  if (result.state === "unknown") {
    return (
      <Frame heading={heading} accountIdHash={accountIdHash} labelled={label !== undefined}>
        <Card title="Account">
          <p className="max-w-prose text-sm text-muted">
            No spend recorded for this account. The hash is well formed, and the account lookup
            hands out a well-formed hash for any account id whether or not it has ever been emitted,
            so this is what both a mistyped id and a customer with no activity in the window look
            like. Nothing here says the account does not exist.
          </p>
        </Card>
      </Frame>
    );
  }

  return (
    <Frame heading={heading} accountIdHash={accountIdHash} labelled={label !== undefined}>
      <Report detail={result.detail} />
    </Frame>
  );
}

function Report({ detail }: { detail: AccountDetail }) {
  const perUser = costPerUser(detail);
  const windowDays = detail.trend.length;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Direct cost">
          <Money micro={detail.directCostMicroUsd} />
        </Stat>
        <Stat label="Users">
          {detail.distinctUsers > 0 ? (
            detail.distinctUsers.toLocaleString()
          ) : (
            <Blank reason="no user id was recorded on this account's spans, so distinct users cannot be counted" />
          )}
        </Stat>
        <Stat label="Spans">
          <span className="tabular-nums">{detail.spanCount.toLocaleString()}</span>
        </Stat>
        <Stat label="Cost per user">
          {perUser.micro === null ? (
            <Blank reason={perUser.reason ?? "not enough data to divide cost by users"} />
          ) : (
            <Money micro={perUser.micro} />
          )}
        </Stat>
      </div>

      <p className="max-w-prose text-sm text-muted">
        Directly attributable spend (LLM, tool calls, vector, embeddings) for this account over the
        last {windowDays} days. Compute and egress are excluded throughout: no span carries an
        account for them, so every figure on this page understates what the account truly costs.
      </p>

      <Card title="Cost by layer">
        <LayerSplit detail={detail} />
      </Card>

      <Card title="Top features by spend">
        <FeatureTable
          features={detail.topFeatures}
          accountDirectMicroUsd={detail.directCostMicroUsd}
        />
      </Card>

      <Card title={`Daily direct cost, last ${windowDays} days`}>
        <TrendChart trend={detail.trend} />
        <TrendReconciliation detail={detail} />
      </Card>

      <Card title="Heaviest runs">
        <RunTable runs={detail.topRuns} />
        <p className="mt-3 max-w-prose text-xs text-muted">
          Costed at this account&apos;s spans only. A run that served several customers carries a
          larger total on its own page, which is the whole run rather than this account&apos;s share
          of it; charging every account the full run would put the same money on several bills.
        </p>
      </Card>
    </div>
  );
}

/**
 * The six-layer split.
 *
 * Compute and egress appear, and they appear as explained blanks rather than as $0.00. Zero is a
 * measurement and these are not measured: no span carries an account for infrastructure spend, so
 * the true statement is "this cannot be attributed per account yet", and printing a zero beside
 * four real numbers reads as "this customer used no compute", which is almost certainly false. The
 * tenant-level figure they are missing from is the excluded-cost banner on the parent tab.
 */
function LayerSplit({ detail }: { detail: AccountDetail }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-muted">
          <tr>
            <th className="py-1 text-left font-medium">Layer</th>
            <th className="py-1 text-right font-medium">Direct cost</th>
            <th className="py-1 text-right font-medium">Share of account</th>
          </tr>
        </thead>
        <tbody>
          {LAYERS.map((layer) => {
            const excluded = EXCLUDED_LAYERS.includes(layer);
            const share = excluded ? null : layerShare(detail, layer as DirectLayer);
            return (
              <tr key={layer} className="border-t border-edge">
                <td className={`py-2 ${excluded ? "text-muted" : "font-medium"}`}>
                  {LAYER_LABEL[layer]}
                </td>
                <td className="py-2 text-right tabular-nums">
                  {excluded ? (
                    <Blank reason={`${LAYER_LABEL[layer]} spend carries no account id, so none of it can be attributed to this account. It is reported at tenant level on the accounts tab.`} />
                  ) : (
                    <Money micro={detail.byLayer[layer as DirectLayer]} />
                  )}
                </td>
                <td className="py-2 text-right tabular-nums">
                  {excluded ? (
                    <Blank reason="excluded from per-account cost, so it has no share of this account's spend" />
                  ) : share === null ? (
                    <Blank reason="this account has no directly attributable spend in the window, so there is no total to take a share of" />
                  ) : (
                    <Pct value={share} />
                  )}
                </td>
              </tr>
            );
          })}
          <tr className="border-t border-edge bg-ink/40 font-medium">
            <td className="py-2">attributable total</td>
            <td className="py-2 text-right tabular-nums">
              <Money micro={detail.directCostMicroUsd} />
            </td>
            <td className="py-2 text-right tabular-nums">
              {detail.directCostMicroUsd > 0 ? (
                "100.0%"
              ) : (
                <Blank reason="no directly attributable spend recorded for this account in the window" />
              )}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

/**
 * States when the chart's own sum disagrees with the total printed above it.
 *
 * These two figures come from separate reads (a per-day group and a per-layer group), so they are
 * two chances to disagree, and the way they historically disagreed was silent: a day list built
 * from the Node clock instead of the window ClickHouse reported is shifted against the SQL window
 * across a midnight boundary, and the oldest day falls out of the chart while still counting toward
 * the total. Nothing on screen showed it. This says it out loud instead of leaving a reader to add
 * up thirty bars by eye.
 */
function TrendReconciliation({ detail }: { detail: AccountDetail }) {
  const charted = trendTotal(detail.trend);
  if (charted === detail.directCostMicroUsd) return null;
  return (
    <p className="mt-2 max-w-prose text-xs text-warn">
      The days above add up to <Money micro={charted} />, which is not the{" "}
      <Money micro={detail.directCostMicroUsd} /> reported for this account. Read the chart as the
      shape of the spend rather than as its total until that is resolved.
    </p>
  );
}

/** Breadcrumb, heading, and copy control. Shared by all four endings so they cannot look broken. */
function Frame({
  heading,
  accountIdHash,
  labelled = false,
  children,
}: {
  heading: string;
  accountIdHash?: string;
  labelled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm text-muted">
        <Link href="/cost-per-customer" className="text-accent hover:underline">
          Cost per customer
        </Link>
        <span>/</span>
        <span>account</span>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <h1
          className={`text-xl font-semibold ${labelled ? "" : "font-mono"}`}
          title={accountIdHash}
        >
          {heading}
        </h1>
        {accountIdHash ? (
          <>
            {!labelled ? (
              <span className="text-xs text-muted" title="no label set for this account">
                unlabelled
              </span>
            ) : null}
            <CopyHashButton accountIdHash={accountIdHash} />
          </>
        ) : null}
      </div>

      {children}
    </div>
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
