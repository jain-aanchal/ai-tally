// SPDX-License-Identifier: Apache-2.0
"use client";

// One interactive guardrail rule row (CTO-120). Three affordances, all POSTing through
// /api/guardrails (which forwards an idempotent change_id to the gateway):
//   - Mode select  — flip enforcement mode; POSTs immediately (optimistic, rolls back on error).
//   - Edit caps    — change the cost / step cap; shows a confirm dialog BEFORE POSTing, because a
//                    cap change alters what trips for live traffic.
//   - Audit        — expand the per-rule change log, read lazily from /api/guardrails?audit=1.

import { useState, useTransition } from "react";

import {
  type GuardrailMode,
  type GuardrailRule,
  GUARDRAIL_MODES,
  GRADUATION_LABEL,
  fireRate,
  graduationSignal,
  modeMeta,
} from "@/lib/guardrails";
import { formatUSD } from "@/lib/types";

const DASH = "—";

interface AuditChange {
  change_id: string;
  rule_id: string;
  actor: string | null;
  changed_at: string;
  before: { state?: string } | null;
  after: { state?: string } | null;
}

function capLabel(rule: GuardrailRule): string {
  const parts: string[] = [];
  if (rule.maxCostMicroUsd != null) parts.push(`${formatUSD(rule.maxCostMicroUsd)}/run`);
  if (rule.maxSteps != null) parts.push(`${rule.maxSteps} steps`);
  return parts.length ? parts.join(" · ") : DASH;
}

async function postRule(rule: GuardrailRule): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/guardrails", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(rule),
    });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { error?: string };
      return { ok: false, error: body.error ?? `save failed (${res.status})` };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

export function GuardrailRow({ initialRule }: { initialRule: GuardrailRule }) {
  const [rule, setRule] = useState(initialRule);
  const [status, setStatus] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  // Cap-edit dialog state.
  const [editing, setEditing] = useState(false);
  const [costInput, setCostInput] = useState("");
  const [stepsInput, setStepsInput] = useState("");

  // Audit-log state (lazy).
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditRows, setAuditRows] = useState<AuditChange[] | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);

  const save = (next: GuardrailRule, okText: string) => {
    const prev = rule;
    setRule(next); // optimistic
    setStatus(null);
    startTransition(async () => {
      const res = await postRule(next);
      if (res.ok) {
        setStatus({ tone: "ok", text: okText });
      } else {
        setRule(prev); // rollback
        setStatus({ tone: "err", text: res.error ?? "Save failed." });
      }
    });
  };

  const onModeChange = (mode: GuardrailMode) => {
    if (mode === rule.mode) return;
    save({ ...rule, mode }, `Mode → ${modeMeta(mode).label}. Live within the refresh window.`);
  };

  const openEdit = () => {
    setCostInput(rule.maxCostMicroUsd != null ? (rule.maxCostMicroUsd / 1_000_000).toString() : "");
    setStepsInput(rule.maxSteps != null ? rule.maxSteps.toString() : "");
    setEditing(true);
  };

  const confirmEdit = () => {
    const costUsd = costInput.trim() === "" ? null : Number(costInput);
    const steps = stepsInput.trim() === "" ? null : Number(stepsInput);
    const maxCostMicroUsd =
      costUsd != null && Number.isFinite(costUsd) && costUsd >= 0
        ? Math.round(costUsd * 1_000_000)
        : null;
    const maxSteps =
      steps != null && Number.isFinite(steps) && steps >= 0 ? Math.round(steps) : null;
    if (maxCostMicroUsd == null && maxSteps == null) {
      setStatus({ tone: "err", text: "A rule must set a cost cap or a step cap." });
      return;
    }
    setEditing(false);
    save({ ...rule, maxCostMicroUsd, maxSteps }, "Caps updated. Live within the refresh window.");
  };

  const toggleAudit = async () => {
    const next = !auditOpen;
    setAuditOpen(next);
    if (next && auditRows === null && !auditLoading) {
      setAuditLoading(true);
      try {
        const res = await fetch(`/api/guardrails?audit=1&rule_id=${encodeURIComponent(rule.id)}`);
        const body = (await res.json()) as { changes?: AuditChange[] };
        setAuditRows(Array.isArray(body.changes) ? body.changes : []);
      } catch {
        setAuditRows([]);
      } finally {
        setAuditLoading(false);
      }
    }
  };

  const meta = modeMeta(rule.mode);
  const signal = graduationSignal(rule);
  const ratePct = Math.round(fireRate(rule) * 100);

  return (
    <>
      <tr className="border-b border-edge/60 align-top">
        <td className="py-3 pr-3">
          <div className="font-medium">{rule.scope}</div>
          <div className="text-xs text-muted">{rule.scopeKind}</div>
        </td>
        <td className="py-3 pr-3">
          <div className="flex items-center gap-2">
            <span>{capLabel(rule)}</span>
            <button
              type="button"
              onClick={openEdit}
              disabled={pending}
              className="rounded border border-edge px-1.5 py-0.5 text-[11px] text-muted hover:bg-edge hover:text-white"
            >
              Edit
            </button>
          </div>
        </td>
        <td className="py-3 pr-3">
          {rule.runsThisWeek > 0 ? (
            <span>
              {rule.wouldHaveFiredThisWeek.toLocaleString()} / {rule.runsThisWeek.toLocaleString()}{" "}
              <span className="text-muted">({ratePct}%)</span>
            </span>
          ) : (
            <span className="text-muted">{DASH}</span>
          )}
        </td>
        <td className="py-3 pr-3">
          {meta.enforcing ? (
            <span className="text-xs text-muted">enforcing</span>
          ) : (
            <span
              className={`text-xs ${signal === "ready" ? "text-accent" : "text-muted"}`}
              title={GRADUATION_LABEL[signal]}
            >
              {GRADUATION_LABEL[signal]}
            </span>
          )}
        </td>
        <td className="py-3 pr-3">
          <select
            value={rule.mode}
            disabled={pending}
            onChange={(e) => onModeChange(e.target.value as GuardrailMode)}
            aria-label={`Mode for ${rule.scope}`}
            className="rounded border border-edge bg-ink/40 px-2 py-1 text-xs text-white disabled:opacity-50"
          >
            {GUARDRAIL_MODES.map((m) => (
              <option key={m.mode} value={m.mode}>
                {m.label}
              </option>
            ))}
          </select>
          {status && (
            <div className={`mt-1 text-[11px] ${status.tone === "ok" ? "text-good" : "text-warn"}`}>
              {status.text}
            </div>
          )}
        </td>
        <td className="py-3 text-right">
          <button
            type="button"
            onClick={toggleAudit}
            aria-expanded={auditOpen}
            className="rounded border border-edge px-2 py-1 text-xs text-muted hover:bg-edge hover:text-white"
          >
            {auditOpen ? "Hide" : "Audit"}
          </button>
        </td>
      </tr>

      {editing && (
        <tr className="border-b border-edge/60 bg-ink/30">
          <td colSpan={6} className="px-2 py-3">
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col text-xs text-muted">
                Cost cap (USD / run)
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={costInput}
                  onChange={(e) => setCostInput(e.target.value)}
                  placeholder="none"
                  className="mt-1 w-32 rounded border border-edge bg-ink/40 px-2 py-1 text-sm text-white"
                />
              </label>
              <label className="flex flex-col text-xs text-muted">
                Step cap
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={stepsInput}
                  onChange={(e) => setStepsInput(e.target.value)}
                  placeholder="none"
                  className="mt-1 w-32 rounded border border-edge bg-ink/40 px-2 py-1 text-sm text-white"
                />
              </label>
              <div className="ml-auto flex items-center gap-2">
                <span className="text-[11px] text-warn">
                  Confirm: this changes what trips for live traffic.
                </span>
                <button
                  type="button"
                  onClick={() => setEditing(false)}
                  className="rounded border border-edge px-2.5 py-1 text-xs text-muted hover:bg-edge hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={confirmEdit}
                  className="rounded border border-accent/50 bg-accent/15 px-2.5 py-1 text-xs font-medium text-accent hover:bg-accent/25"
                >
                  Confirm &amp; save
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}

      {auditOpen && (
        <tr className="border-b border-edge/60 bg-ink/20">
          <td colSpan={6} className="px-2 py-3">
            {auditLoading ? (
              <span className="text-xs text-muted">Loading audit log…</span>
            ) : auditRows && auditRows.length > 0 ? (
              <ul className="space-y-1 text-xs">
                {auditRows.map((c) => (
                  <li key={c.change_id} className="flex flex-wrap gap-2 text-muted">
                    <span className="text-white">{c.changed_at || "—"}</span>
                    <span>
                      {(c.before?.state ?? "∅")} → {(c.after?.state ?? "∅")}
                    </span>
                    {c.actor && <span>by {c.actor}</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-xs text-muted">
                No audit entries (the control plane may be unreachable in this environment).
              </span>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
