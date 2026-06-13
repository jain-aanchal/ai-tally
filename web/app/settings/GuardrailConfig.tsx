// SPDX-License-Identifier: Apache-2.0
"use client";

import { useState } from "react";

import {
  CONFIG_REFRESH_SECONDS,
  GRADUATION_LABEL,
  GUARDRAIL_MODES,
  type GuardrailMode,
  type GuardrailRule,
  type GuardrailScopeKind,
  fireRate,
  graduationSignal,
  isActionable,
  modeMeta,
  summarize,
} from "@/lib/guardrails";

let _tmp = 0;
function tempId(): string {
  _tmp += 1;
  return `gr_new_${Date.now()}_${_tmp}`;
}

function microToDollarStr(micro: number | null): string {
  return micro == null ? "" : String(micro / 1_000_000);
}

function dollarStrToMicro(s: string): number | null {
  const t = s.trim();
  if (t === "") return null;
  const usd = Number(t);
  if (!Number.isFinite(usd) || usd < 0) return null;
  return Math.round(usd * 1_000_000);
}

function intStrOrNull(s: string): number | null {
  const t = s.trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isInteger(n) || n < 0) return null;
  return n;
}

type SaveState = "idle" | "saving" | "saved" | "error";

export function GuardrailConfig({ initialRules }: { initialRules: GuardrailRule[] }) {
  const [rules, setRules] = useState<GuardrailRule[]>(initialRules);
  const [saving, setSaving] = useState<Record<string, SaveState>>({});
  const summary = summarize(rules);

  function patch(id: string, changes: Partial<GuardrailRule>) {
    setRules((rs) => rs.map((r) => (r.id === id ? { ...r, ...changes } : r)));
    setSaving((s) => ({ ...s, [id]: "idle" }));
  }

  async function save(rule: GuardrailRule) {
    setSaving((s) => ({ ...s, [rule.id]: "saving" }));
    try {
      const res = await fetch("/api/guardrails", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(rule),
      });
      setSaving((s) => ({ ...s, [rule.id]: res.ok ? "saved" : "error" }));
    } catch {
      setSaving((s) => ({ ...s, [rule.id]: "error" }));
    }
  }

  function addRule() {
    const r: GuardrailRule = {
      id: tempId(),
      scopeKind: "agent",
      scope: "",
      mode: "observe",
      maxCostMicroUsd: 1_000_000,
      maxSteps: null,
      wouldHaveFiredThisWeek: 0,
      runsThisWeek: 0,
    };
    setRules((rs) => [...rs, r]);
  }

  function removeRule(id: string) {
    setRules((rs) => rs.filter((r) => r.id !== id));
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-4 gap-3">
        <Stat label="Guardrails" value={summary.total} />
        <Stat label="Enforcing" value={summary.enforcing} />
        <Stat label="Observe-only" value={summary.observing} />
        <Stat label="Ready to enforce" value={summary.readyToGraduate} accent />
      </div>

      <p className="text-xs text-muted">
        Every rule starts in observe-only — the engine records what would have fired without touching
        the agent. Mode changes reach the SDK within the {CONFIG_REFRESH_SECONDS}s config-refresh
        window.
      </p>

      <div className="space-y-3">
        {rules.map((rule) => (
          <RuleRow
            key={rule.id}
            rule={rule}
            saveState={saving[rule.id] ?? "idle"}
            onPatch={(c) => patch(rule.id, c)}
            onSave={() => save(rule)}
            onRemove={() => removeRule(rule.id)}
          />
        ))}
      </div>

      <button
        type="button"
        onClick={addRule}
        className="rounded-md border border-dashed border-edge px-3 py-2 text-sm text-muted hover:border-accent hover:text-accent"
      >
        + Add guardrail
      </button>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <div className="rounded-lg border border-edge bg-panel p-3">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${accent ? "text-accent" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function RuleRow({
  rule,
  saveState,
  onPatch,
  onSave,
  onRemove,
}: {
  rule: GuardrailRule;
  saveState: SaveState;
  onPatch: (changes: Partial<GuardrailRule>) => void;
  onSave: () => void;
  onRemove: () => void;
}) {
  const meta = modeMeta(rule.mode);
  const observing = !meta.enforcing;
  const signal = graduationSignal(rule);
  const actionable = isActionable(rule);

  return (
    <section className="rounded-xl border border-edge bg-panel p-4">
      <div className="flex flex-wrap items-end gap-4">
        <Field label="Scope">
          <div className="flex gap-1">
            <select
              aria-label="scope kind"
              value={rule.scopeKind}
              onChange={(e) =>
                onPatch({ scopeKind: e.target.value as GuardrailScopeKind })
              }
              className="rounded-md border border-edge bg-ink px-2 py-1 text-sm"
            >
              <option value="agent">agent</option>
              <option value="feature">feature</option>
            </select>
            <input
              aria-label="scope name"
              value={rule.scope}
              placeholder="name"
              onChange={(e) => onPatch({ scope: e.target.value })}
              className="w-40 rounded-md border border-edge bg-ink px-2 py-1 font-mono text-sm"
            />
          </div>
        </Field>

        <Field label="Cost cap / run ($)">
          <input
            aria-label="cost cap"
            inputMode="decimal"
            defaultValue={microToDollarStr(rule.maxCostMicroUsd)}
            placeholder="none"
            onChange={(e) => onPatch({ maxCostMicroUsd: dollarStrToMicro(e.target.value) })}
            className="w-28 rounded-md border border-edge bg-ink px-2 py-1 text-sm tabular-nums"
          />
        </Field>

        <Field label="Step cap / run">
          <input
            aria-label="step cap"
            inputMode="numeric"
            defaultValue={rule.maxSteps ?? ""}
            placeholder="none"
            onChange={(e) => onPatch({ maxSteps: intStrOrNull(e.target.value) })}
            className="w-24 rounded-md border border-edge bg-ink px-2 py-1 text-sm tabular-nums"
          />
        </Field>

        <Field label="Mode">
          <select
            aria-label="mode"
            value={rule.mode}
            onChange={(e) => onPatch({ mode: e.target.value as GuardrailMode })}
            className="rounded-md border border-edge bg-ink px-2 py-1 text-sm"
          >
            {GUARDRAIL_MODES.map((m) => (
              <option key={m.mode} value={m.mode}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>

        <div className="ml-auto flex items-center gap-2">
          <SaveBadge state={saveState} />
          <button
            type="button"
            disabled={!actionable || !rule.scope}
            onClick={onSave}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:cursor-not-allowed disabled:opacity-40"
          >
            Save
          </button>
          <button
            type="button"
            onClick={onRemove}
            aria-label="remove guardrail"
            className="rounded-md border border-edge px-2 py-1.5 text-sm text-muted hover:text-bad"
          >
            ✕
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        <span className="text-muted">{meta.blurb}</span>
        {!actionable && (
          <span className="rounded bg-bad/20 px-1.5 py-0.5 text-bad">
            set a cost cap or a step cap
          </span>
        )}
        {observing && actionable && (
          <ObserveStats rule={rule} signal={signal} />
        )}
      </div>
    </section>
  );
}

function ObserveStats({
  rule,
  signal,
}: {
  rule: GuardrailRule;
  signal: ReturnType<typeof graduationSignal>;
}) {
  const rate = fireRate(rule);
  const cls =
    signal === "ready"
      ? "bg-good/20 text-good"
      : signal === "noisy"
        ? "bg-bad/20 text-bad"
        : "bg-edge text-muted";
  return (
    <>
      <span className="rounded bg-edge px-1.5 py-0.5 tabular-nums text-gray-200">
        would have fired{" "}
        <strong>{rule.wouldHaveFiredThisWeek.toLocaleString()}</strong> ×/wk
        {rule.runsThisWeek > 0 && <> ({(rate * 100).toFixed(1)}% of runs)</>}
      </span>
      <span className={`rounded px-1.5 py-0.5 ${cls}`}>{GRADUATION_LABEL[signal]}</span>
    </>
  );
}

function SaveBadge({ state }: { state: SaveState }) {
  if (state === "saving") return <span className="text-xs text-muted">saving…</span>;
  if (state === "saved") return <span className="text-xs text-good">saved ✓</span>;
  if (state === "error") return <span className="text-xs text-bad">save failed</span>;
  return null;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wide text-muted">{label}</span>
      {children}
    </label>
  );
}
