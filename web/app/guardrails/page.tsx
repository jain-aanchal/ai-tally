// SPDX-License-Identifier: Apache-2.0
// Guardrails page (CTO-120): renders the tenant's guardrail rules from the control plane (gateway
// GET /v1/tenant/guardrails, via /api/guardrails — falling back to the typed mock when the gateway
// is unreachable). Each rule is interactive: flip its enforcement mode, edit its caps (behind a
// confirm dialog), and inspect its audit log. Edits POST through /api/guardrails, which forwards an
// idempotent change_id to the gateway; the SDK picks the change up on its next config-refresh window.

import { Card } from "@/components/Card";
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

export default async function GuardrailsPage() {
  const { rules, configRefreshSeconds } = await apiGet<GuardrailsPayload>("/api/guardrails");
  const summary = summarize(rules);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Guardrails</h1>
        <p className="mt-1 text-sm text-muted">
          Per-tenant cost / step caps. Rules start in observe-only and graduate to enforcement with
          confidence. Mode changes take effect on the SDK within the {configRefreshSeconds}s
          config-refresh window.
        </p>
      </div>

      <Card title="Summary">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted">Rules</dt>
            <dd className="mt-0.5 text-2xl font-semibold">{summary.total}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted">Enforcing</dt>
            <dd className="mt-0.5 text-2xl font-semibold">{summary.enforcing}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted">Observing</dt>
            <dd className="mt-0.5 text-2xl font-semibold">{summary.observing}</dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted">Ready to enforce</dt>
            <dd className="mt-0.5 text-2xl font-semibold text-accent">{summary.readyToGraduate}</dd>
          </div>
        </dl>
      </Card>

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
