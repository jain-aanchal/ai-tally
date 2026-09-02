// SPDX-License-Identifier: Apache-2.0
"use client";

// Create, edit and withdraw budgets (CTO-208, F4). The storage and every rule it enforces came from
// CTO-205; this is the first surface that lets anyone use it without hand-writing JSON.
//
// THREE THINGS THIS COMPONENT IS DELIBERATE ABOUT.
//
// 1. The overlap 409 is treated as a normal answer, not a crash. The gateway refuses a budget that
//    covers the same scope and period over an overlapping range and NAMES the budget it collided
//    with. That name is shown verbatim and turned into a button that loads the colliding budget
//    into the form, because "that overlaps with research-agent-2026" is only actionable if you can
//    then go and change research-agent-2026.
//
// 2. Editing is the same POST as creating. The gateway upserts on (tenant_id, budget_id) and
//    excludes the row from its own overlap check, so raising an amount needs no delete first. The
//    budget_id field is therefore locked while editing: changing it would silently create a second
//    budget rather than rename the one on screen.
//
// 3. An empty table says "no budget set" and nothing else. It is not an error and it is not a
//    budget of zero. A stored zero is a separate, deliberate claim and renders as $0.00.

import { useState, useTransition } from "react";

import { DataTable, type Column } from "@/components/DataTable";
import { Money } from "@/components/HonestValue";
import { type Budget, microToDollarInput, scopeLabel } from "@/lib/budgetsShared";

import { type BudgetFormValues, deleteBudgetAction, saveBudgetAction } from "./actions";

interface Props {
  initialBudgets: Budget[];
  /** Echoed by the gateway so this form cannot drift from the CHECK constraints in migration 0026. */
  periods: string[];
  scopeKinds: string[];
  /** false when the list could not be read. The empty table then means "we could not ask", which
   *  is a different sentence from "no budget set" and has to read as one. */
  reachable: boolean;
}

type Status =
  | { tone: "ok"; text: string }
  | { tone: "err"; text: string; conflictingBudgetId?: string | null }
  | null;

function blankForm(): BudgetFormValues {
  return {
    budgetId: "",
    period: "month",
    amountDollars: "",
    scopeKind: "tenant",
    scopeValue: "",
    startsOn: "",
    endsOn: "",
  };
}

function formFor(budget: Budget): BudgetFormValues {
  return {
    budgetId: budget.budgetId,
    period: budget.period,
    // The exact stored amount, not the display rounding: an edit-and-save must not change a number
    // the form never showed.
    amountDollars: microToDollarInput(budget.amountMicro),
    scopeKind: budget.scopeKind,
    scopeValue: budget.scopeValue,
    startsOn: budget.startsOn,
    endsOn: budget.endsOn ?? "",
  };
}

export function BudgetManager({ initialBudgets, periods, scopeKinds, reachable }: Props) {
  const [budgets, setBudgets] = useState<Budget[]>(initialBudgets);
  const [form, setForm] = useState<BudgetFormValues | null>(null);
  /** Non-null while editing an existing row: the budget_id is then fixed. */
  const [editing, setEditing] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>(null);
  const [pending, startTransition] = useTransition();

  const set = (patch: Partial<BudgetFormValues>) =>
    setForm((f) => (f ? { ...f, ...patch } : f));

  const openCreate = () => {
    setStatus(null);
    setEditing(null);
    setForm(blankForm());
  };

  const openEdit = (budget: Budget) => {
    setStatus(null);
    setEditing(budget.budgetId);
    setForm(formFor(budget));
  };

  /** The "edit the budget you collided with" path off a 409. */
  const openConflicting = (budgetId: string) => {
    const target = budgets.find((b) => b.budgetId === budgetId);
    if (target) openEdit(target);
    else setStatus({ tone: "err", text: `budget ${budgetId} is not in this tenant's list` });
  };

  const submit = () => {
    if (!form) return;
    setStatus(null);
    startTransition(async () => {
      const res = await saveBudgetAction(form);
      if (res.ok) {
        if (res.budgets) setBudgets(res.budgets);
        setStatus({ tone: "ok", text: `Saved ${form.budgetId}.` });
        setForm(null);
        setEditing(null);
      } else {
        setStatus({
          tone: "err",
          text: res.error ?? "the gateway refused the write",
          conflictingBudgetId: res.conflictingBudgetId,
        });
      }
    });
  };

  const remove = (budgetId: string) => {
    setStatus(null);
    startTransition(async () => {
      const res = await deleteBudgetAction(budgetId);
      setConfirmDelete(null);
      if (res.ok) {
        if (res.budgets) setBudgets(res.budgets);
        setStatus({ tone: "ok", text: `Withdrew ${budgetId}. That scope has no budget set again.` });
        if (editing === budgetId) {
          setForm(null);
          setEditing(null);
        }
      } else {
        setStatus({ tone: "err", text: res.error ?? "the gateway refused the delete" });
      }
    });
  };

  // Built inline rather than memoized: the action cells close over `pending` and `confirmDelete`,
  // and a memo whose deps have to list every one of those is a stale-button bug waiting to happen.
  // A budget list is a handful of rows, so there is nothing here worth caching.
  const columns: Column<Budget>[] = [
    {
      key: "budgetId",
      header: "Budget",
      sortValue: (b) => b.budgetId,
      render: (b) => <span className="font-medium">{b.budgetId}</span>,
    },
    {
      key: "scope",
      header: "Scope",
      sortValue: (b) => scopeLabel(b),
      render: (b) => <span className="text-muted">{scopeLabel(b)}</span>,
    },
    {
      key: "period",
      header: "Period",
      sortValue: (b) => b.period,
      render: (b) => <span className="capitalize">{b.period}</span>,
    },
    {
      key: "amount",
      header: "Amount",
      align: "right",
      sortValue: (b) => b.amountMicro,
      // Money, not a hand-formatted number: a stored 0 has to read as $0.00 (a real claim) and
      // never as a blank, which in this app means "we do not know".
      render: (b) => <Money micro={b.amountMicro} />,
    },
    {
      key: "startsOn",
      header: "Starts",
      sortValue: (b) => b.startsOn,
      render: (b) => <span className="tabular-nums">{b.startsOn}</span>,
    },
    {
      key: "endsOn",
      header: "Ends",
      sortValue: (b) => b.endsOn,
      // NOT a Blank. A Blank in this app means "we do not have this value"; an absent ends_on is
      // a definite statement that the budget stands until somebody changes it.
      render: (b) =>
        b.endsOn ? (
          <span className="tabular-nums">{b.endsOn}</span>
        ) : (
          <span className="text-muted">Open-ended</span>
        ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (b) => (
        <div className="flex items-center justify-end gap-1.5">
          <button
            type="button"
            onClick={() => openEdit(b)}
            className="rounded-full border border-edge bg-ink/40 px-2.5 py-1 text-xs font-medium text-muted transition hover:bg-ink/60"
          >
            Edit
          </button>
          {confirmDelete === b.budgetId ? (
            <button
              type="button"
              disabled={pending}
              onClick={() => remove(b.budgetId)}
              className="rounded-full border border-warn/50 bg-warn/15 px-2.5 py-1 text-xs font-medium text-warn transition hover:bg-warn/25 disabled:opacity-50"
            >
              Confirm withdraw
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(b.budgetId)}
              className="rounded-full border border-edge bg-ink/40 px-2.5 py-1 text-xs font-medium text-muted transition hover:bg-ink/60"
            >
              Withdraw
            </button>
          )}
        </div>
      ),
    },
  ];

  const scopedKind = form && form.scopeKind !== "tenant";

  return (
    <div className="space-y-4">
      <DataTable
        columns={columns}
        rows={budgets}
        rowKey={(b) => b.budgetId}
        pageSize={25}
        initialSort={{ key: "budgetId", direction: "asc" }}
        empty={
          reachable ? (
            <span>
              No budget set. That is a normal state, not a gap: nothing here is assumed to be zero,
              and spend surfaces show no variance until a budget exists.
            </span>
          ) : (
            <span>
              Budgets could not be read, so this table says nothing about your configuration. You
              may well have budgets set.
            </span>
          )
        }
      />

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={openCreate}
          className="rounded-full border border-accent/50 bg-accent/15 px-3 py-1 text-xs font-medium text-accent transition hover:bg-accent/25"
        >
          Set a budget
        </button>
        {status && (
          <span
            className={`text-[11px] ${status.tone === "ok" ? "text-good" : "text-warn"}`}
            role="status"
          >
            {status.text}
            {status.tone === "err" && status.conflictingBudgetId ? (
              <button
                type="button"
                onClick={() => openConflicting(status.conflictingBudgetId as string)}
                className="ml-2 rounded border border-edge px-1.5 py-0.5 text-[11px] text-muted hover:text-fg"
              >
                Edit {status.conflictingBudgetId}
              </button>
            ) : null}
          </span>
        )}
      </div>

      {form && (
        <div className="rounded-lg border border-edge bg-ink/60 p-4">
          <h3 className="text-sm font-medium">
            {editing ? `Edit ${editing}` : "New budget"}
          </h3>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Field
              label="Budget id"
              hint={
                editing
                  ? "Fixed while editing. A different id would create a second budget, not rename this one."
                  : "Your own stable handle, for example research-agent-2026. It appears in overlap errors."
              }
            >
              <input
                id="budget-id"
                type="text"
                value={form.budgetId}
                disabled={editing !== null}
                placeholder="research-agent-2026"
                onChange={(e) => set({ budgetId: e.target.value })}
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60 disabled:opacity-60"
              />
            </Field>

            <Field label="Amount (USD)" hint="Stored as integer micro-USD. Zero is allowed and means this scope may spend nothing.">
              <input
                id="budget-amount"
                type="text"
                inputMode="decimal"
                value={form.amountDollars}
                placeholder="30000"
                onChange={(e) => set({ amountDollars: e.target.value })}
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60"
              />
            </Field>

            <Field label="Period" hint="Only the periods the forecast can evaluate a window for.">
              <select
                id="budget-period"
                value={form.period}
                onChange={(e) => set({ period: e.target.value })}
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60"
              >
                {periods.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Scope" hint="Tenant-wide is the whole bill. The other kinds name one member of a dimension the cost queries already group by.">
              <select
                id="budget-scope-kind"
                value={form.scopeKind}
                onChange={(e) =>
                  set({
                    scopeKind: e.target.value,
                    // Switching to tenant-wide clears the value: a tenant budget must name nothing.
                    scopeValue: e.target.value === "tenant" ? "" : form.scopeValue,
                  })
                }
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60"
              >
                {scopeKinds.map((k) => (
                  <option key={k} value={k}>
                    {k === "tenant" ? "tenant (whole bill)" : k}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label="Scope value"
              hint={
                scopedKind
                  ? "A feature id, model id or cost layer, matching your telemetry exactly. Case is preserved."
                  : "Not used for a tenant-wide budget."
              }
            >
              <input
                id="budget-scope-value"
                type="text"
                value={form.scopeValue}
                disabled={!scopedKind}
                placeholder={scopedKind ? "research-agent" : ""}
                onChange={(e) => set({ scopeValue: e.target.value })}
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60 disabled:opacity-40"
              />
            </Field>

            <Field label="Starts on" hint="Inclusive.">
              <input
                id="budget-starts-on"
                type="date"
                value={form.startsOn}
                onChange={(e) => set({ startsOn: e.target.value })}
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60"
              />
            </Field>

            <Field
              label="Ends on (optional)"
              hint="Leave blank for open-ended, the usual case. Inclusive: a budget ending on the 31st covers the 31st, so its successor starts on the 1st."
            >
              <input
                id="budget-ends-on"
                type="date"
                value={form.endsOn}
                onChange={(e) => set({ endsOn: e.target.value })}
                className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60"
              />
            </Field>
          </div>

          <div className="mt-4 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setForm(null);
                setEditing(null);
              }}
              className="rounded border border-edge px-2 py-1 text-xs text-muted hover:bg-ink/60"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={submit}
              disabled={pending}
              className="rounded border border-accent/50 bg-accent/15 px-2 py-1 text-xs font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
            >
              {pending ? "Saving…" : "Save budget"}
            </button>
          </div>

          <p className="mt-3 max-w-prose text-[10px] leading-snug text-muted">
            Two budgets for the same scope and period cannot cover the same day. An overlapping save
            is refused and names the budget it collided with. Adjacent ranges are fine, and raising
            an existing budget is an edit rather than a second row.
          </p>
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <span className="block text-[11px] font-medium text-muted">{label}</span>
      {children}
      {hint && <p className="mt-1 text-[10px] leading-snug text-muted">{hint}</p>}
    </div>
  );
}
