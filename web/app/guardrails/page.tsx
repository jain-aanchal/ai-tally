// SPDX-License-Identifier: Apache-2.0
// Guardrails page (CTO-120): renders the tenant's guardrail rules from the control plane (gateway
// GET /v1/tenant/guardrails, via /api/guardrails — falling back to the typed mock when the gateway
// is unreachable). Each rule is interactive: flip its enforcement mode, edit its caps (behind a
// confirm dialog), and inspect its audit log. Edits POST through /api/guardrails, which forwards an
// idempotent change_id to the gateway; the SDK picks the change up on its next config-refresh window.

import { Card } from "@/components/Card";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";
import {
  type GuardrailRule,
  GUARDRAIL_MODES,
  summarize,
} from "@/lib/guardrails";
import { GuardrailRow } from "./GuardrailRow";

interface GuardrailsPayload {
  rules: GuardrailRule[];
  configRefreshSeconds: number;
}

// A count stat rendered in the kit's tile shell (CTO-225). These are integer counts, not money, so
// they intentionally do not go through SummaryTile/<Money>; there is no honest-blank case for a
// count of rules the page already holds in memory.
function CountTile({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-edge bg-panel p-4">
      <span className="text-xs font-medium uppercase tracking-wide text-muted">{label}</span>
      <span className={`text-2xl font-semibold tabular-nums ${accent ? "text-accent" : ""}`}>
        {value}
      </span>
    </div>
  );
}

export default async function GuardrailsPage() {
  const { rules, configRefreshSeconds } = await apiGet<GuardrailsPayload>("/api/guardrails");
  const summary = summarize(rules);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Guardrails"
        subtitle={
          <>
            Per-tenant cost / step caps. Rules start in observe-only and graduate to enforcement
            with confidence. Mode changes take effect on the SDK within the {configRefreshSeconds}s
            config-refresh window.
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <CountTile label="Rules" value={summary.total} />
        <CountTile label="Enforcing" value={summary.enforcing} />
        <CountTile label="Observing" value={summary.observing} />
        <CountTile label="Ready to enforce" value={summary.readyToGraduate} accent />
      </div>

      <Card title="Rules">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-edge text-left text-xs uppercase tracking-wide text-muted">
                <th className="pb-2 pr-3 font-medium">Scope</th>
                <th className="pb-2 pr-3 font-medium">Caps</th>
                <th className="pb-2 pr-3 font-medium">Would-fire / wk</th>
                <th className="pb-2 pr-3 font-medium">Graduation</th>
                <th className="pb-2 pr-3 font-medium">Mode</th>
                <th className="pb-2 font-medium text-right">Audit</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <GuardrailRow key={rule.id} initialRule={rule} />
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-muted">
          Modes (weakest → strongest):{" "}
          {GUARDRAIL_MODES.map((m) => m.label).join(" · ")}. Only{" "}
          {GUARDRAIL_MODES.filter((m) => m.enforcing).length} of {GUARDRAIL_MODES.length} alter the
          agent; observe-only never does.
        </p>
      </Card>
    </div>
  );
}
