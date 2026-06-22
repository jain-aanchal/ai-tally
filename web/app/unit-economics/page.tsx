// SPDX-License-Identifier: Apache-2.0
// Unit Economics page (CTO-121): renders CAC flavors / payback / LTV from the already-wired libs
// (web/lib/cac.ts + web/lib/unitEconomics.ts). Read-only. Band colors come from the lib's
// `ltvCacBand` helper — NO hardcoded thresholds live in this file.

import { Card } from "@/components/Card";
import { SyntheticPreviewBanner } from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import type { CacPayload } from "@/app/api/cac/route";
import type { CacPeriod, PeriodEconomics } from "@/lib/cac";
import {
  blendedCac,
  fullyLoadedCac,
  ltv,
  ltvCacBand,
  ltvOverCac,
  marginPerUser,
  marketingCac,
  paybackMonths,
} from "@/lib/unitEconomics";
import { formatUSD } from "@/lib/types";

const DASH = "—";

/** Monthly contribution margin per account = ARPA × gross margin. Null when economics is missing. */
function monthlyMargin(econ: PeriodEconomics | undefined): number | null {
  if (!econ) return null;
  const value = econ.arpaMicroUsd;
  const cost = econ.arpaMicroUsd * (1 - econ.grossMarginPct);
  return marginPerUser(value, cost);
}

function fmtMoney(v: number | null): string {
  return v === null ? DASH : formatUSD(v);
}

function fmtMonths(v: number | null): string {
  return v === null ? DASH : `${v.toFixed(1)} mo`;
}

function fmtRatio(v: number | null): string {
  return v === null ? DASH : `${v.toFixed(2)}×`;
}

function fmtPct(v: number | null): string {
  return v === null ? DASH : `${Math.round(v * 100)}%`;
}

function fmtMonthLabel(periodStart: string): string {
  // periodStart is "YYYY-MM-01"; render "Mon YYYY" without timezone drift.
  const [y, m] = periodStart.split("-").map((s) => parseInt(s, 10));
  const d = new Date(Date.UTC(y, m - 1, 1));
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
}

/** Tailwind text color for a band from the lib's `ltvCacBand` classifier. */
function bandText(band: ReturnType<typeof ltvCacBand>): string {
  return band === "green"
    ? "text-good"
    : band === "yellow"
      ? "text-warn"
      : band === "red"
        ? "text-bad"
        : "text-muted";
}

export default async function UnitEconomicsPage() {
  const data = await apiGet<CacPayload>("/api/cac");
  const { periods, economics, isMock } = data;

  // Headline cards reflect the most recent period for which we have both CAC inputs and economics.
  // Falling to the latest period regardless keeps the CAC flavors visible even when economics is
  // missing (payback/LTV then honest-null to "—").
  const latest: CacPeriod | undefined = periods[0];
  const latestEcon = latest ? economics[latest.periodStart] : undefined;

  const blended = latest ? blendedCac(latest) : null;
  const paid = latest ? marketingCac(latest) : null; // "paid CAC" = paid spend / paid customers
  const loaded = latest ? fullyLoadedCac(latest) : null;
  const margin = monthlyMargin(latestEcon);
  const payback = paybackMonths(loaded, margin);
  const ltvValue = ltv(margin, latestEcon?.retentionMonths ?? 0);
  const ratio = ltvOverCac(ltvValue, loaded);
  const band = ltvCacBand(ratio);

  const body = (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Metric label="Blended CAC" value={fmtMoney(blended)} hint="(paid+sales+content) / new customers" />
        <Metric label="Paid CAC" value={fmtMoney(paid)} hint="paid spend / new paid customers" />
        <Metric label="Payback" value={fmtMonths(payback)} hint="fully-loaded CAC / monthly margin" />
        <Metric label="LTV" value={fmtMoney(ltvValue)} hint="margin × expected retention" />
        <Metric
          label="LTV : CAC"
          value={fmtRatio(ratio)}
          valueClass={bandText(band)}
          hint="vs. fully-loaded CAC"
        />
      </div>

      <Card title="Monthly history">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Period</th>
                <th className="py-1 text-right font-medium">Marketing spend</th>
                <th className="py-1 text-right font-medium">Sales spend</th>
                <th className="py-1 text-right font-medium">New (total)</th>
                <th className="py-1 text-right font-medium">New (paid)</th>
                <th className="py-1 text-right font-medium">ARPA</th>
                <th className="py-1 text-right font-medium">Gross margin</th>
                <th className="py-1 text-right font-medium">Blended CAC</th>
                <th className="py-1 text-right font-medium">Paid CAC</th>
                <th className="py-1 text-right font-medium">Payback</th>
                <th className="py-1 text-right font-medium">LTV : CAC</th>
              </tr>
            </thead>
            <tbody>
              {periods.map((p) => {
                const econ = economics[p.periodStart];
                const m = monthlyMargin(econ);
                const loadedRow = fullyLoadedCac(p);
                const ltvRow = ltv(m, econ?.retentionMonths ?? 0);
                const ratioRow = ltvOverCac(ltvRow, loadedRow);
                const bandRow = ltvCacBand(ratioRow);
                return (
                  <tr
                    key={p.periodStart}
                    className={`border-t border-edge ${p.locked ? "text-muted" : ""}`}
                  >
                    <td className="py-2 font-medium">
                      <span className={p.locked ? "" : "text-white"}>{fmtMonthLabel(p.periodStart)}</span>
                      {p.locked && (
                        <span
                          className="ml-2 rounded bg-edge px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted"
                          title={`Closed${p.closedAt ? ` ${p.closedAt.slice(0, 10)}` : ""} — locked, not editable`}
                        >
                          Locked
                        </span>
                      )}
                    </td>
                    <td className="py-2 text-right tabular-nums">{formatUSD(p.paidSpendMicroUsd + p.contentSpendMicroUsd)}</td>
                    <td className="py-2 text-right tabular-nums">{formatUSD(p.salesSpendMicroUsd)}</td>
                    <td className="py-2 text-right tabular-nums">{p.newCustomersTotal.toLocaleString()}</td>
                    <td className="py-2 text-right tabular-nums">{p.newCustomersPaid.toLocaleString()}</td>
                    <td className="py-2 text-right tabular-nums">{econ ? formatUSD(econ.arpaMicroUsd) : DASH}</td>
                    <td className="py-2 text-right tabular-nums">{econ ? fmtPct(econ.grossMarginPct) : DASH}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMoney(blendedCac(p))}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMoney(marketingCac(p))}</td>
                    <td className="py-2 text-right tabular-nums">{fmtMonths(paybackMonths(loadedRow, m))}</td>
                    <td className={`py-2 text-right tabular-nums ${bandText(bandRow)}`}>{fmtRatio(ratioRow)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-muted">
          Marketing spend = paid + content. Locked months are prior-month-closed and not editable.
          Payback / LTV need ARPA and gross margin; periods missing either show {DASH}.
        </p>
      </Card>

      {/* CSV upload deferred (CTO-121): the gateway exposes /v1/tenant/cac/csv, but wiring an
          authenticated file-upload proxy + optimistic UI is its own change. Read-only render first.
          TODO(CTO-121-followup): add a file-upload button that POSTs to a thin /api/cac proxy. */}
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Unit Economics</h1>
          <p className="mt-1 text-sm text-muted">
            CAC by flavor, payback, and LTV — honest under uncertainty. Undefined metrics render {DASH}.
          </p>
        </div>
      </div>

      {isMock ? (
        <SyntheticPreviewBanner workflow="Unit Economics">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
  valueClass,
}: {
  label: string;
  value: string;
  hint: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-xl border border-edge bg-panel p-5">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-2 text-2xl font-semibold tabular-nums ${valueClass ?? ""}`}>{value}</div>
      <div className="mt-2 text-xs text-muted">{hint}</div>
    </div>
  );
}
