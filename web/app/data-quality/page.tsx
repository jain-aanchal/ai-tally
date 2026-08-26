// SPDX-License-Identifier: Apache-2.0
import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import { asOfLabel, deriveDataState, relativeAge } from "@/lib/dataState";
import {
  classify,
  classifyAccountStitching,
  withheldByConflicts,
  type AccountStitching,
  type DataQualityReport,
  type Health,
} from "@/lib/dq";
import { formatUSD } from "@/lib/types";

export default async function DataQualityPage() {
  const dq = await apiGet<DataQualityReport>("/api/data-quality");
  const { overall, attribution, contextDrops, calibration, sampling, accountStitching } = dq;

  // Latest reconciled calibration day is the freshness boundary; absence ⇒ pre-data.
  const reconciledThrough = calibration.length > 0 ? calibration[calibration.length - 1].date : "1970-01-01";
  const noData =
    attribution.length === 0 && calibration.length === 0 && overall.attributionRate === 0;
  const someUnattributed =
    attribution.some((a) => a.events7d === 0) && attribution.some((a) => a.events7d > 0);
  const state = deriveDataState({
    isEmpty: noData,
    isPartial: someUnattributed,
    reconciledThrough,
  });
  const asOf = asOfLabel(reconciledThrough);

  const body = (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <KpiCard
          label="Attribution rate"
          value={`${Math.round(overall.attributionRate * 100)}%`}
          health={classify("attribution", overall.attributionRate)}
          hint="business events successfully tied to a trace"
        />
        <KpiCard
          label="Context drops (24h)"
          value={overall.contextDropCount24h.toLocaleString()}
          health={classify("drops", overall.contextDropCount24h)}
          hint="spans missing an active trace context — detectable, never silent"
        />
        <KpiCard
          label="Estimate calibration"
          value={`${(overall.estimateCalibration * 100).toFixed(1)}% off`}
          health={classify("calibration", overall.estimateCalibration)}
          hint="|estimated − reconciled| / reconciled for the last reconciled period"
        />
        <KpiCard
          label="Effective sample rate"
          value={`${Math.round(overall.effectiveSampleRate * 100)}%`}
          health="good"
          hint="weighted across strata — tail kept ~100%, body sampled down"
        />
      </div>

      <Card title="Attribution by feature">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">Feature</th>
              <th className="py-1 text-right font-medium">Rate</th>
              <th className="py-1 text-right font-medium">Events (7d)</th>
            </tr>
          </thead>
          <tbody>
            {attribution.map((a) => {
              const h = classify("attribution", a.events7d === 0 ? 1 : a.rate);
              return (
                <tr key={a.feature} className="border-t border-edge">
                  <td className="py-2 font-medium">{a.feature}</td>
                  <td className="py-2 text-right tabular-nums">
                    {a.events7d === 0 ? (
                      <span className="text-muted">no value event</span>
                    ) : (
                      <HealthText h={h}>{Math.round(a.rate * 100)}%</HealthText>
                    )}
                  </td>
                  <td className="py-2 text-right tabular-nums">{a.events7d.toLocaleString()}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      <Card title="Context drops by service">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">Service</th>
              <th className="py-1 text-left font-medium">SDK</th>
              <th className="py-1 text-right font-medium">Drops (24h)</th>
            </tr>
          </thead>
          <tbody>
            {contextDrops.map((d) => {
              // CTO-118: distinguish "service inactive" (no spans in 24h) from "real zero drops".
              // Inactive → "—" with tooltip; active+zero → green "0"; active+>0 → red count.
              const inactive = (d.spans24h ?? 1) === 0;
              return (
                <tr key={`${d.service}-${d.sdkVersion}`} className="border-t border-edge">
                  <td className="py-2">{d.service}</td>
                  <td className="py-2 font-mono text-xs text-gray-300">{d.sdkVersion}</td>
                  <td className="py-2 text-right tabular-nums">
                    {inactive ? (
                      <span className="text-muted" title="no spans in the last 24h">—</span>
                    ) : (
                      <HealthText h={classify("drops", d.drops24h)}>
                        {d.drops24h.toLocaleString()}
                      </HealthText>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      <AccountStitchingCard stitching={accountStitching} />

      <Card title="Estimate vs. reconciled — last 7 reconciled days">
        <table className="w-full text-sm">
          <thead className="text-xs uppercase text-muted">
            <tr>
              <th className="py-1 text-left font-medium">Date</th>
              <th className="py-1 text-right font-medium">Estimated</th>
              <th className="py-1 text-right font-medium">Reconciled</th>
              <th className="py-1 text-right font-medium">Δ</th>
            </tr>
          </thead>
          <tbody>
            {calibration.map((c) => {
              const diff = c.estimatedMicroUsd - c.reconciledMicroUsd;
              const pct = diff / c.reconciledMicroUsd;
              const h = classify("calibration", Math.abs(pct));
              return (
                <tr key={c.date} className="border-t border-edge">
                  <td className="py-2 font-mono text-xs text-gray-300">{c.date}</td>
                  <td className="py-2 text-right tabular-nums">{formatUSD(c.estimatedMicroUsd)}</td>
                  <td className="py-2 text-right tabular-nums">{formatUSD(c.reconciledMicroUsd)}</td>
                  <td className="py-2 text-right tabular-nums">
                    <HealthText h={h}>
                      {pct > 0 ? "+" : ""}
                      {(pct * 100).toFixed(1)}%
                    </HealthText>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      {/* CTO-119: hide the table entirely when no sampling is configured (effective rate == 100%
          AND no spans in any stratum) — every span is captured, the CI is degenerate, the table
          would just be confusing. Inactive tenants (no data) get the same treatment. */}
      {(() => {
        const totalSpans = sampling.reduce((s, r) => s + r.spans, 0);
        if (overall.effectiveSampleRate >= 1.0 && totalSpans === 0) {
          return (
            <Card title="Sampling by stratum">
              <p className="text-sm text-muted" title="no sampling configured — every span captured">
                Not applicable — every span captured.
              </p>
            </Card>
          );
        }
        return (
          <Card title="Sampling by stratum">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase text-muted">
                <tr>
                  <th className="py-1 text-left font-medium">Stratum</th>
                  <th className="py-1 text-right font-medium">Keep rate</th>
                  <th className="py-1 text-right font-medium">CI on extrapolated cost</th>
                  <th className="py-1 text-right font-medium">Spans (30d)</th>
                </tr>
              </thead>
              <tbody>
                {sampling.map((s) => (
                  <tr key={s.stratum} className="border-t border-edge">
                    <td className="py-2 capitalize">{s.stratum}</td>
                    <td className="py-2 text-right tabular-nums">{Math.round(s.rate * 100)}%</td>
                    <td className="py-2 text-right tabular-nums">
                      {s.ciHalfWidthPct === null ? (
                        <span className="text-muted" title="needs ≥30 kept spans in 30d">
                          —
                        </span>
                      ) : s.ciHalfWidthPct === 0 ? (
                        <span className="text-good">exact</span>
                      ) : (
                        `±${Math.round(s.ciHalfWidthPct * 100)}%`
                      )}
                    </td>
                    <td className="py-2 text-right tabular-nums">{s.spans.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        );
      })()}
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Data Quality</h1>
          <p className="mt-1 text-sm text-muted">
            Every number we show, and how confident you can be in it. Honest under uncertainty.
          </p>
        </div>
        {state !== "empty" && asOf && (
          <StaleBadge asOf={asOf} age={relativeAge(reconciledThrough)} stale={state === "stale"} />
        )}
      </div>

      {state === "partial" && <PartialDataBanner missing="a value-event source for every feature" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Data Quality">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function KpiCard({
  label,
  value,
  health,
  hint,
}: {
  label: string;
  value: string;
  health: Health;
  hint: string;
}) {
  const ring =
    health === "good"
      ? "border-good/40"
      : health === "warn"
        ? "border-warn/40"
        : "border-bad/40";
  return (
    <div className={`rounded-xl border ${ring} bg-panel p-5`}>
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-2 text-3xl font-semibold tabular-nums ${textFor(health)}`}>{value}</div>
      <div className="mt-2 text-xs text-muted">{hint}</div>
    </div>
  );
}

/**
 * Account stitching, and the users we refuse to attribute (CTO-184).
 *
 * Two jobs. First, separate directly-tagged accounts from stitched ones, because they are not
 * equally trustworthy: a direct account was stamped on the span at emit time, a stitched one was
 * inferred from a CRM or CDP assertion and is only as good as that connector.
 *
 * Second, and the reason this card exists at all: one user belongs to one account. When a user
 * turns up against two, the pipeline attributes nothing for them rather than splitting the cost,
 * duplicating it, or picking first-seen, because duplication inflates the tenant total and breaks
 * reconciliation against /cost. That refusal is correct but it must not be quiet. The fix is in
 * the tenant's CRM, so the table names the ambiguous users, both candidate accounts, and the
 * spend being held back, which is the number that tells them whether it is worth fixing.
 */
function AccountStitchingCard({ stitching }: { stitching?: AccountStitching }) {
  // No account dimension instrumented, or an older payload without the field: say so plainly.
  // Not a warning and not a fabricated zero. Nothing is wrong, there is just nothing there.
  if (!stitching || (stitching.directAccounts === 0 && stitching.stitchedAccounts === 0)) {
    return (
      <Card title="Account stitching">
        <p className="text-sm text-muted">
          No account dimension yet. Tag spans with <code className="font-mono">account_id</code>,
          or connect a CRM or CDP that asserts one, and coverage appears here.
        </p>
      </Card>
    );
  }

  const { directAccounts, stitchedAccounts, stitchedUsers, conflicts } = stitching;
  const health = classifyAccountStitching(stitching);
  const withheld = withheldByConflicts(conflicts);

  return (
    <Card title="Account stitching">
      <div className="grid grid-cols-1 gap-4 text-sm sm:grid-cols-3">
        <Stat
          label="Tagged directly"
          value={directAccounts.toLocaleString()}
          hint="account_id on the span itself, as certain as the span"
        />
        <Stat
          label="Stitched from identity"
          value={stitchedAccounts.toLocaleString()}
          hint={`inferred from CRM/CDP account_id edges across ${stitchedUsers.toLocaleString()} users`}
        />
        <Stat
          label="Users withheld"
          value={<HealthText h={health}>{conflicts.length.toLocaleString()}</HealthText>}
          hint="observed against more than one account, so nothing is attributed for them"
        />
      </div>

      {conflicts.length > 0 && (
        <>
          <p className="mt-5 text-sm text-bad">
            {conflicts.length.toLocaleString()}{" "}
            {conflicts.length === 1 ? "user belongs" : "users belong"} to more than one account, so{" "}
            {formatUSD(withheld)} of their spend is attributed to no account at all. One user
            belongs to one account: we will not split or duplicate this, because duplicating would
            inflate your tenant total and stop per-account spend reconciling with Cost. Resolve the
            duplicate membership in the source system and it attributes on the next pass.
          </p>
          <table className="mt-3 w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">User</th>
                <th className="py-1 text-left font-medium">Accounts observed</th>
                <th className="py-1 text-right font-medium">Withheld (30d)</th>
                <th className="py-1 text-right font-medium">Spans (30d)</th>
              </tr>
            </thead>
            <tbody>
              {conflicts.map((c) => (
                <tr key={c.userIdHash} className="border-t border-edge">
                  <td className="py-2 font-mono text-xs text-gray-300" title={c.userIdHash}>
                    {c.userIdHash.slice(0, 12)}…
                  </td>
                  <td className="py-2 font-mono text-xs text-gray-300" title={c.accounts.join(", ")}>
                    {c.accounts.map((a) => a.slice(0, 8)).join(" · ")}
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    <HealthText h="bad">{formatUSD(c.withheldMicroUsd)}</HealthText>
                  </td>
                  <td className="py-2 text-right tabular-nums">{c.spans30d.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Card>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint: string;
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      <div className="mt-1 text-xs text-muted">{hint}</div>
    </div>
  );
}

function HealthText({ h, children }: { h: Health; children: React.ReactNode }) {
  return <span className={textFor(h)}>{children}</span>;
}

function textFor(h: Health): string {
  return h === "good" ? "text-good" : h === "warn" ? "text-warn" : "text-bad";
}
