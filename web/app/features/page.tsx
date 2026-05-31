import { Card } from "@/components/Card";
import {
  PartialDataBanner,
  StaleBadge,
  SyntheticPreviewBanner,
} from "@/components/DataStateBanner";
import { apiGet } from "@/lib/api";
import { deriveDataState, relativeAge, STALE_AFTER_MS } from "@/lib/dataState";
import {
  type AttributionDiagnostics,
  type FeatureEconomics,
  margin,
} from "@/lib/features";
import { formatUSD } from "@/lib/types";

interface FeaturesPayload {
  features: FeatureEconomics[];
  diagnostics: AttributionDiagnostics;
}

export default async function FeaturesPage() {
  const { features, diagnostics } = await apiGet<FeaturesPayload>("/api/features");

  // Features has no reconciliation date; the reconciler's last-run minutes is its freshness signal.
  const reconciledThrough = new Date(
    Date.now() - diagnostics.reconcilerLastRunMinutesAgo * 60_000,
  ).toISOString();
  const noEconomics = features.length === 0 || features.every((f) => f.costPerUserMicroUsd === 0);
  const someUnattributed =
    features.some((f) => f.valueEvent === null) && features.some((f) => f.valueEvent !== null);
  const state = deriveDataState({
    isEmpty: noEconomics,
    isPartial: someUnattributed,
    reconciledThrough,
  });
  const reconcilerStale = diagnostics.reconcilerLastRunMinutesAgo * 60_000 > STALE_AFTER_MS;

  const body = (
    <div className="space-y-6">
      <Card title="Unit economics — per feature">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Feature</th>
                <th className="py-1 text-right font-medium">Cost/user</th>
                <th className="py-1 text-right font-medium">Value/user</th>
                <th className="py-1 text-right font-medium">Margin</th>
                <th className="py-1 text-right font-medium">Payback</th>
                <th className="py-1 text-right font-medium">Attribution rate</th>
                <th className="py-1 pl-3 text-left font-medium">Value event</th>
              </tr>
            </thead>
            <tbody>
              {features.map((f) => {
                const m = margin(f);
                return (
                  <tr key={f.feature} className="border-t border-edge">
                    <td className="py-2 font-medium">{f.feature}</td>
                    <td className="py-2 text-right tabular-nums">{formatUSD(f.costPerUserMicroUsd)}</td>
                    <td className="py-2 text-right tabular-nums">
                      {f.valuePerUserMicroUsd === null ? <Dash /> : formatUSD(f.valuePerUserMicroUsd)}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {m === null ? <Dash /> : <MarginCell m={m} />}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {f.paybackDays === null ? <Dash /> : `${f.paybackDays}d`}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {f.attributionRate === null ? <Dash /> : `${Math.round(f.attributionRate * 100)}%`}
                    </td>
                    <td className="py-2 pl-3">
                      {f.valueEvent === null ? (
                        <span className="text-warn text-xs">configure value event →</span>
                      ) : (
                        <span className="font-mono text-xs text-gray-300">{f.valueEvent}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Attribution diagnostics">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-xs uppercase text-muted">Per-feature confidence breakdown</h3>
            <ul className="space-y-3 text-sm">
              {features
                .filter((f) => f.attributionRate !== null)
                .map((f) => (
                  <li key={f.feature}>
                    <div className="mb-1 flex items-baseline justify-between">
                      <span className="font-medium">{f.feature}</span>
                      <span className="tabular-nums text-muted">
                        {Math.round((f.attributionRate ?? 0) * 100)}% attributed
                      </span>
                    </div>
                    <ConfidenceBar b={f.attributionBreakdown} />
                  </li>
                ))}
            </ul>
            <Legend />
          </div>

          <div>
            <h3 className="mb-2 text-xs uppercase text-muted">Tenant-wide</h3>
            <dl className="space-y-1.5 text-sm">
              <Diag k="late-arriving events (7d)" v={diagnostics.lateArrivalEvents7d.toLocaleString()} />
              <Diag k="median lag" v={`${diagnostics.lateArrivalMedianHours.toFixed(1)}h`} />
              <Diag
                k="reconciler last ran"
                v={`${diagnostics.reconcilerLastRunMinutesAgo} min ago`}
                good={!reconcilerStale}
              />
            </dl>
          </div>
        </div>
      </Card>
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Features</h1>
        {state !== "empty" && (
          <StaleBadge
            asOf={relativeAge(reconciledThrough)}
            age={relativeAge(reconciledThrough)}
            stale={state === "stale"}
          />
        )}
      </div>

      {state === "partial" && <PartialDataBanner missing="value-event tracking for every feature" />}

      {state === "empty" ? (
        <SyntheticPreviewBanner workflow="Features">{body}</SyntheticPreviewBanner>
      ) : (
        body
      )}
    </div>
  );
}

function Dash() {
  return <span className="text-muted">—</span>;
}

function MarginCell({ m }: { m: number }) {
  const pct = Math.round(m * 100);
  return <span className={m > 0.5 ? "text-good" : m > 0 ? "" : "text-bad"}>{pct}%</span>;
}

function ConfidenceBar({
  b,
}: {
  b: { direct: number; sessionStitched: number; identityGraphStitched: number; unmatched: number };
}) {
  const total = b.direct + b.sessionStitched + b.identityGraphStitched + b.unmatched;
  if (total === 0) return null;
  const seg = (n: number) => `${(n / total) * 100}%`;
  return (
    <div className="flex h-2 w-full overflow-hidden rounded bg-edge">
      <div style={{ width: seg(b.direct) }} className="bg-good" title={`direct: ${b.direct}`} />
      <div style={{ width: seg(b.sessionStitched) }} className="bg-accent" title={`session: ${b.sessionStitched}`} />
      <div
        style={{ width: seg(b.identityGraphStitched) }}
        className="bg-warn"
        title={`identity graph: ${b.identityGraphStitched}`}
      />
      <div style={{ width: seg(b.unmatched) }} className="bg-bad/70" title={`unmatched: ${b.unmatched}`} />
    </div>
  );
}

function Legend() {
  const dot = (cls: string) => <span aria-hidden className={`inline-block h-2 w-2 rounded-sm ${cls}`} />;
  return (
    <ul className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
      <li className="flex items-center gap-1.5">{dot("bg-good")} direct</li>
      <li className="flex items-center gap-1.5">{dot("bg-accent")} session</li>
      <li className="flex items-center gap-1.5">{dot("bg-warn")} identity graph</li>
      <li className="flex items-center gap-1.5">{dot("bg-bad/70")} unmatched</li>
    </ul>
  );
}

function Diag({ k, v, good }: { k: string; v: string; good?: boolean }) {
  return (
    <div className="flex items-baseline justify-between">
      <dt className="text-muted">{k}</dt>
      <dd className={good ? "text-good" : ""}>{v}</dd>
    </div>
  );
}
