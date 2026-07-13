// SPDX-License-Identifier: Apache-2.0
"use client";

import { useState } from "react";

import type { UnitEconomicsThresholds } from "@/lib/unitEconomics";

type SaveState = "idle" | "saving" | "saved" | "error";

const FIELDS: {
  key: keyof UnitEconomicsThresholds;
  label: string;
  hint: string;
  step: string;
}[] = [
  { key: "ltvCacGreen", label: "LTV:CAC green above", hint: "ratio strictly above → green", step: "0.1" },
  { key: "ltvCacYellow", label: "LTV:CAC yellow at/above", hint: "at/above → yellow, below → red", step: "0.1" },
  { key: "paybackGreen", label: "Payback green ≤ (mo)", hint: "months at/below → green", step: "1" },
  { key: "paybackYellow", label: "Payback yellow ≤ (mo)", hint: "at/below → yellow, above → red", step: "1" },
];

function invalid(t: UnitEconomicsThresholds): string | null {
  for (const { key, label } of FIELDS) {
    const v = t[key];
    if (!Number.isFinite(v) || v < 0) return `${label} must be a number ≥ 0`;
  }
  if (t.ltvCacGreen < t.ltvCacYellow) return "LTV:CAC green cutoff must be ≥ the yellow cutoff";
  if (t.paybackGreen > t.paybackYellow) return "Payback green cutoff must be ≤ the yellow cutoff";
  return null;
}

/**
 * Settings panel to edit the tenant's LTV/CAC band thresholds (CTO-126). Seeded with the tenant's
 * resolved thresholds; "Reset to defaults" restores the hardcoded B2B-SaaS values (and saving them
 * makes the fallback explicit). Persists via POST /api/unit-economics/config (idempotent upsert).
 */
export function ThresholdSettings({
  initial,
  defaults,
  hasOverride,
}: {
  initial: UnitEconomicsThresholds;
  defaults: UnitEconomicsThresholds;
  hasOverride: boolean;
}) {
  const [form, setForm] = useState<UnitEconomicsThresholds>(initial);
  const [state, setState] = useState<SaveState>("idle");
  const [overridden, setOverridden] = useState(hasOverride);
  const err = invalid(form);
  const isDefault =
    form.ltvCacGreen === defaults.ltvCacGreen &&
    form.ltvCacYellow === defaults.ltvCacYellow &&
    form.paybackGreen === defaults.paybackGreen &&
    form.paybackYellow === defaults.paybackYellow;

  function patch(key: keyof UnitEconomicsThresholds, raw: string) {
    const n = raw.trim() === "" ? NaN : Number(raw);
    setForm((f) => ({ ...f, [key]: n }));
    setState("idle");
  }

  function resetToDefaults() {
    setForm(defaults);
    setState("idle");
  }

  async function save() {
    if (err) return;
    setState("saving");
    try {
      const res = await fetch("/api/unit-economics/config", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(form),
      });
      if (res.ok) {
        setState("saved");
        setOverridden(true);
      } else {
        setState("error");
      }
    } catch {
      setState("error");
    }
  }

  return (
    <section className="rounded-xl border border-edge bg-panel p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">Band thresholds</h2>
          <p className="mt-1 text-xs text-muted">
            Cutoffs that color the LTV:CAC ratio and payback. {overridden && !isDefault
              ? "Tenant override active."
              : "Using defaults."}
          </p>
        </div>
        <button
          type="button"
          onClick={resetToDefaults}
          disabled={isDefault}
          className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
        >
          Reset to defaults
        </button>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {FIELDS.map(({ key, label, hint, step }) => (
          <label key={key} className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wide text-muted">{label}</span>
            <input
              aria-label={label}
              type="number"
              step={step}
              min="0"
              value={Number.isFinite(form[key]) ? form[key] : ""}
              onChange={(e) => patch(key, e.target.value)}
              className="w-full rounded-md border border-edge bg-ink px-2 py-1 text-sm tabular-nums"
            />
            <span className="text-[11px] text-muted">{hint}</span>
          </label>
        ))}
      </div>

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={!!err || state === "saving"}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:cursor-not-allowed disabled:opacity-40"
        >
          Save thresholds
        </button>
        {err && <span className="text-xs text-bad">{err}</span>}
        {!err && state === "saving" && <span className="text-xs text-muted">saving…</span>}
        {!err && state === "saved" && <span className="text-xs text-good">saved ✓</span>}
        {!err && state === "error" && <span className="text-xs text-bad">save failed</span>}
      </div>
    </section>
  );
}
