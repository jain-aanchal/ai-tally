import { Card } from "@/components/Card";
import { Legend, StackedBarChart } from "@/components/StackedBarChart";
import { apiGet } from "@/lib/api";
import {
  LAYER_LABEL,
  LAYERS,
  type CostSeries,
  estimatedTotal,
  type FeatureCostRow,
  type HiddenCostAlert,
  type Layer,
  reconciledTotal,
  totalRange,
} from "@/lib/cost";
import { formatUSD, type SpendByLayer } from "@/lib/types";

interface CostPayload {
  series: CostSeries;
  featureRows: FeatureCostRow[];
  alerts: HiddenCostAlert[];
}

function sumLayer(rows: FeatureCostRow[], layer: Layer) {
  return rows.reduce((s, r) => s + r.byLayer[layer], 0);
}

export default async function CostPage() {
  const { series: costSeries, featureRows, alerts: hiddenCostAlerts } =
    await apiGet<CostPayload>("/api/cost");
  const total = totalRange(costSeries);
  const reconciled = reconciledTotal(costSeries);
  const estimated = estimatedTotal(costSeries);

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Cost</h1>

      <Card title="Cost by layer — last 14 days">
        <div className="mb-2 flex items-baseline gap-3 text-sm">
          <span className="text-2xl font-semibold">{formatUSD(total)}</span>
          <span className="text-muted">
            reconciled {formatUSD(reconciled)} (through {costSeries.reconciledThrough}) · estimated {formatUSD(estimated)}
          </span>
        </div>
        <StackedBarChart series={costSeries} />
        <Legend />
      </Card>

      {hiddenCostAlerts.map((a) => (
        <div
          key={a.message}
          className={`rounded-xl border p-4 text-sm ${
            a.severity === "warn"
              ? "border-warn/40 bg-warn/10 text-warn"
              : "border-edge bg-panel text-muted"
          }`}
        >
          <span className="font-medium">Hidden cost: </span>
          {a.message}
        </div>
      ))}

      <Card title="By feature">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Feature</th>
                {LAYERS.map((l) => (
                  <th key={l} className="py-1 text-right font-medium">
                    {LAYER_LABEL[l]}
                  </th>
                ))}
                <th className="py-1 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {featureRows.map((r) => {
                const t = LAYERS.reduce((s, l) => s + r.byLayer[l], 0);
                return (
                  <tr key={r.feature} className="border-t border-edge">
                    <td className="py-2 font-medium">{r.feature}</td>
                    {LAYERS.map((l) => (
                      <td key={l} className="py-2 text-right tabular-nums">
                        {formatUSD(r.byLayer[l])}
                      </td>
                    ))}
                    <td className="py-2 text-right tabular-nums">{formatUSD(t)}</td>
                  </tr>
                );
              })}
              <FooterRow rows={featureRows} />
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function FooterRow({ rows }: { rows: FeatureCostRow[] }) {
  const totals = LAYERS.reduce<SpendByLayer>(
    (acc, l) => {
      acc[l] = sumLayer(rows, l);
      return acc;
    },
    { llm: 0, vector: 0, tools: 0, compute: 0, embeddings: 0, egress: 0 },
  );
  const grand = LAYERS.reduce((s, l) => s + totals[l], 0);
  return (
    <tr className="border-t border-edge bg-ink/40 font-medium">
      <td className="py-2">all features</td>
      {LAYERS.map((l) => (
        <td key={l} className="py-2 text-right tabular-nums">
          {formatUSD(totals[l])}
        </td>
      ))}
      <td className="py-2 text-right tabular-nums">{formatUSD(grand)}</td>
    </tr>
  );
}
