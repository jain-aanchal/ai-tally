import { Card } from "@/components/Card";
import { apiGet } from "@/lib/api";
import type { CostOutlier, DataQuality, FeatureRoi, SpendSummary } from "@/lib/types";
import { formatUSD } from "@/lib/types";

interface HomePayload {
  spend: SpendSummary;
  outliers: CostOutlier[];
  roi: FeatureRoi[];
  dq: DataQuality;
}

export default async function HomePage() {
  const { spend: s, outliers, roi, dq } = await apiGet<HomePayload>("/api/home");
  const hidden = s.byLayer.vector + s.byLayer.tools + s.byLayer.compute + s.byLayer.embeddings + s.byLayer.egress;
  const hiddenPct = Math.round((hidden / s.totalMicroUsd) * 100);

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Home</h1>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Spend — last 30 days">
          <div className="text-3xl font-semibold">{formatUSD(s.totalMicroUsd)}</div>
          <div className="mt-2 text-sm text-muted">
            estimated {formatUSD(s.estimatedMicroUsd)} · reconciled {formatUSD(s.reconciledMicroUsd)}
            <span className="ml-1 text-xs">(through {s.reconciledThrough})</span>
          </div>
          <div className="mt-3 text-sm text-warn">
            Hidden cost: {formatUSD(hidden)} ({hiddenPct}%) — vector + tools + compute
          </div>
        </Card>

        <Card title="Top cost outliers (24h)">
          <ul className="space-y-2 text-sm">
            {outliers.map((o) => (
              <li key={o.runId} className="flex items-center justify-between gap-3">
                <span className="truncate font-mono text-gray-300">{o.runId}</span>
                <span className="shrink-0">
                  <span className="font-medium">{formatUSD(o.costMicroUsd)}</span>{" "}
                  <span className="text-bad">{o.multipleOfMedian}× median</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="ROI snapshot">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Feature</th>
                <th className="py-1 text-right font-medium">Cost/user</th>
                <th className="py-1 text-right font-medium">Value/user</th>
                <th className="py-1 text-right font-medium">Payback</th>
              </tr>
            </thead>
            <tbody>
              {roi.map((r) => (
                <tr key={r.feature} className="border-t border-edge">
                  <td className="py-1.5">{r.feature}</td>
                  <td className="py-1.5 text-right">{formatUSD(r.costPerUserMicroUsd)}</td>
                  <td className="py-1.5 text-right">
                    {r.valuePerUserMicroUsd === null ? (
                      <span className="text-muted">—</span>
                    ) : (
                      formatUSD(r.valuePerUserMicroUsd)
                    )}
                  </td>
                  <td className="py-1.5 text-right">
                    {r.paybackDays === null ? (
                      <span className="text-muted">unattributed</span>
                    ) : (
                      `${r.paybackDays}d`
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card title="Data quality">
          <dl className="space-y-2 text-sm">
            <Metric label="attribution rate" value={`${Math.round(dq.attributionRate * 100)}%`} good />
            <Metric label="context drops" value={String(dq.contextDropCount)} good={dq.contextDropCount === 0} />
            <Metric label="estimate calibration" value={`${(dq.estimateCalibration * 100).toFixed(1)}% off`} good={dq.estimateCalibration < 0.03} />
          </dl>
        </Card>
      </div>
    </div>
  );
}

function Metric({ label, value, good }: { label: string; value: string; good: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted">{label}</dt>
      <dd className={good ? "text-good" : "text-warn"}>{value}</dd>
    </div>
  );
}
