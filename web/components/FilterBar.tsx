// SPDX-License-Identifier: Apache-2.0
// The shared dashboard filter toolbar (CTO-221, D1). Time range, group-by, and multi-select
// dimension filters, all URL-synced through useFilters so the bar is stateless: it reads and writes
// the query string and nothing else. Pages adopt it later; this ticket only builds it.
//
// Dimension filter OPTIONS are a prop, not something the bar fetches: which providers or models
// exist is page data (it comes from the same query the page already runs), so a page passes the
// values it knows and the bar renders a control only for the dimensions it was given. A dimension
// with no options simply shows no filter, never an empty menu.
//
// Theme: chrome is driven by the Tailwind semantic tokens (edge / panel / muted / accent) and the
// chart CSS variables, so it is correct in dark today and tracks a light palette if one is applied.

"use client";

import { useEffect, useRef, useState } from "react";

import {
  DEFAULT_GROUP_BY,
  DIMENSION_LABEL,
  DIMENSIONS,
  type Dimension,
  type TimeRangePreset,
} from "@/lib/filters";
import { useFilters } from "@/lib/useFilters";

/** A selectable value for a dimension filter. `label` defaults to `value` when omitted. */
export interface FilterOption {
  value: string;
  label?: string;
}

export interface FilterBarProps {
  /** Available values per dimension. A dimension absent here renders no filter control. */
  options?: Partial<Record<Dimension, FilterOption[]>>;
  /** Restrict the group-by choices. Defaults to every dimension. */
  groupByChoices?: readonly Dimension[];
  /** Hide the group-by selector entirely (a page that groups by a fixed dimension). */
  hideGroupBy?: boolean;
  /**
   * The group-by a page falls back to when the URL carries none (CTO-224). The global default is
   * `layer`, which is not in every page's `groupByChoices` (Compare offers only model/provider), so
   * without this the select value would not match any option. When the active group-by is outside
   * `groupByChoices`, the bar commits this default to the URL once so the state, the select and the
   * chart that reads the same group-by all agree.
   */
  defaultGroupBy?: Dimension;
}

const RANGE_PRESETS: { preset: Exclude<TimeRangePreset, "custom">; label: string }[] = [
  { preset: "7d", label: "7d" },
  { preset: "30d", label: "30d" },
  { preset: "90d", label: "90d" },
];

export function FilterBar({
  options,
  groupByChoices = DIMENSIONS,
  hideGroupBy,
  defaultGroupBy,
}: FilterBarProps) {
  const {
    state,
    setRangePreset,
    setCustomRange,
    setGroupBy,
    toggleFilter,
    clearFilter,
    clearAll,
  } = useFilters();

  const anyActive = DIMENSIONS.some((d) => state.filters[d].length > 0);

  // If the active group-by is not one this page offers, adopt the page default so the select value
  // is always a real option and the explore chart (which reads the same group-by from the URL) groups
  // by the page's natural dimension rather than the global `layer` default. One commit, then the
  // guard is false and it never loops. Only fires when a page both restricts choices and names a
  // default; a bare FilterBar keeps the foundation's every-dimension behavior untouched.
  const groupByValid = groupByChoices.includes(state.groupBy);
  const fallbackGroupBy = defaultGroupBy ?? DEFAULT_GROUP_BY;
  useEffect(() => {
    if (!hideGroupBy && !groupByValid && groupByChoices.includes(fallbackGroupBy)) {
      setGroupBy(fallbackGroupBy);
    }
  }, [hideGroupBy, groupByValid, fallbackGroupBy, groupByChoices, setGroupBy]);
  const selectedGroupBy = groupByValid ? state.groupBy : fallbackGroupBy;

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-edge bg-panel px-3 py-2 text-sm">
      {/* Time range */}
      <div className="flex items-center gap-1" role="group" aria-label="Time range">
        {RANGE_PRESETS.map(({ preset, label }) => (
          <button
            key={preset}
            type="button"
            aria-pressed={state.range.preset === preset}
            onClick={() => setRangePreset(preset)}
            className={segmentClass(state.range.preset === preset)}
          >
            {label}
          </button>
        ))}
        <CustomRangeControl
          active={state.range.preset === "custom"}
          from={state.range.from}
          to={state.range.to}
          onApply={setCustomRange}
        />
      </div>

      <Divider />

      {!hideGroupBy && (
        <label className="flex items-center gap-1.5 text-muted">
          <span className="uppercase text-xs tracking-wide">Group by</span>
          <select
            value={selectedGroupBy}
            onChange={(e) => setGroupBy(e.target.value as Dimension)}
            className="rounded-md border border-edge bg-ink px-2 py-1 text-fg focus:border-accent focus:outline-none"
          >
            {groupByChoices.map((dim) => (
              <option key={dim} value={dim}>
                {DIMENSION_LABEL[dim]}
              </option>
            ))}
          </select>
        </label>
      )}

      {/* Dimension filters, one dropdown per dimension the page supplied options for. */}
      {DIMENSIONS.filter((d) => (options?.[d]?.length ?? 0) > 0).map((dim) => (
        <FilterDropdown
          key={dim}
          dim={dim}
          options={options![dim]!}
          selected={state.filters[dim]}
          onToggle={(value) => toggleFilter(dim, value)}
          onClear={() => clearFilter(dim)}
        />
      ))}

      {anyActive && (
        <button
          type="button"
          onClick={clearAll}
          className="ml-auto rounded-md px-2 py-1 text-xs text-muted hover:text-fg"
        >
          Clear filters
        </button>
      )}
    </div>
  );
}

function segmentClass(active: boolean): string {
  return [
    "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
    active ? "bg-accent text-white" : "text-muted hover:text-fg",
  ].join(" ");
}

function Divider() {
  return <span aria-hidden className="mx-1 h-5 w-px bg-edge" />;
}

function CustomRangeControl({
  active,
  from,
  to,
  onApply,
}: {
  active: boolean;
  from: string | null;
  to: string | null;
  onApply: (from: string, to: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Draft dates default to today so the two inputs are never blank-then-invalid on first open.
  const today = new Date().toISOString().slice(0, 10);
  const [draftFrom, setDraftFrom] = useState(from ?? today);
  const [draftTo, setDraftTo] = useState(to ?? today);

  return (
    <div className="relative">
      <button
        type="button"
        aria-pressed={active}
        onClick={() => setOpen((v) => !v)}
        className={segmentClass(active)}
      >
        {active && from && to ? `${from.slice(5)} – ${to.slice(5)}` : "Custom"}
      </button>
      {open && (
        <div className="absolute left-0 top-full z-10 mt-1 flex flex-col gap-2 rounded-lg border border-edge bg-panel p-3 shadow-lg">
          <label className="flex items-center justify-between gap-2 text-xs text-muted">
            From
            <input
              type="date"
              value={draftFrom}
              max={draftTo}
              onChange={(e) => setDraftFrom(e.target.value)}
              className="rounded-md border border-edge bg-ink px-2 py-1 text-fg"
            />
          </label>
          <label className="flex items-center justify-between gap-2 text-xs text-muted">
            To
            <input
              type="date"
              value={draftTo}
              min={draftFrom}
              onChange={(e) => setDraftTo(e.target.value)}
              className="rounded-md border border-edge bg-ink px-2 py-1 text-fg"
            />
          </label>
          <button
            type="button"
            onClick={() => {
              onApply(draftFrom, draftTo);
              setOpen(false);
            }}
            className="rounded-md bg-accent px-2 py-1 text-xs font-medium text-white hover:opacity-90"
          >
            Apply
          </button>
        </div>
      )}
    </div>
  );
}

function FilterDropdown({
  dim,
  options,
  selected,
  onToggle,
  onClear,
}: {
  dim: Dimension;
  options: FilterOption[];
  selected: string[];
  onToggle: (value: string) => void;
  onClear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on an outside click so several dropdowns don't stack open at once.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const count = selected.length;
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={[
          "flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium",
          count > 0
            ? "border-accent/60 bg-accent/10 text-fg"
            : "border-edge text-muted hover:text-fg",
        ].join(" ")}
      >
        {DIMENSION_LABEL[dim]}
        {count > 0 && (
          <span className="rounded-full bg-accent px-1.5 text-[10px] leading-4 text-white">
            {count}
          </span>
        )}
        <span aria-hidden className="text-[10px]">
          ▾
        </span>
      </button>
      {open && (
        <div className="absolute left-0 top-full z-10 mt-1 max-h-64 w-56 overflow-y-auto rounded-lg border border-edge bg-panel p-1 shadow-lg">
          {options.map((opt) => {
            const checked = selected.includes(opt.value);
            return (
              <label
                key={opt.value}
                className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs text-fg hover:bg-edge"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onToggle(opt.value)}
                  className="accent-accent"
                />
                <span className="truncate">{opt.label ?? opt.value}</span>
              </label>
            );
          })}
          {count > 0 && (
            <button
              type="button"
              onClick={onClear}
              className="mt-1 w-full rounded-md px-2 py-1 text-left text-xs text-muted hover:text-fg"
            >
              Clear {DIMENSION_LABEL[dim].toLowerCase()}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
