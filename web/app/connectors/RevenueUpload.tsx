// SPDX-License-Identifier: Apache-2.0
"use client";

// CSV revenue upload card (CTO-198). Modelled on ConnectForm: the form posts through a server
// action to the gateway, which owns every validation rule.
//
// Two things this UI has to make impossible to miss.
//
// 1. The number is a SNAPSHOT. Each period carries the date it was uploaded and the shared
//    StaleBadge goes amber once nobody has refreshed it, so a six-month-old spreadsheet can never
//    read like a live feed.
// 2. A rejected file names its LINE NUMBERS. The gateway refuses the whole file on a single bad
//    row (a partial accept would replace a complete period with an incomplete one), so the panel
//    lists every offending line at once rather than making the operator find them one at a time.
import { useRef, useState, useTransition } from "react";

import { StaleBadge } from "@/components/DataStateBanner";
import {
  type RevenueSnapshot,
  type UploadRowError,
  deriveUploadFreshness,
  formatSnapshotAmount,
} from "@/lib/revenueUpload";
import { deleteRevenueUploadAction, uploadRevenueCsvAction } from "./revenueUploadActions";

interface Props {
  snapshots: RevenueSnapshot[];
  /** True when the gateway could not be reached. We show why rather than an empty table. */
  unreachable: boolean;
}

export function RevenueUpload({ snapshots, unreachable }: Props) {
  const [rows, setRows] = useState<RevenueSnapshot[]>(snapshots);
  const [status, setStatus] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [rowErrors, setRowErrors] = useState<UploadRowError[]>([]);
  const [pending, startTransition] = useTransition();
  const fileRef = useRef<HTMLInputElement>(null);

  const freshness = deriveUploadFreshness(rows);

  const onFile = async (file: File) => {
    setStatus(null);
    setRowErrors([]);
    const text = await file.text();
    startTransition(async () => {
      const result = await uploadRevenueCsvAction(text, file.name);
      if (fileRef.current) fileRef.current.value = "";
      if (!result.ok) {
        setStatus({ tone: "err", text: result.error });
        setRowErrors(result.rowErrors);
        return;
      }
      // Replace, never append — the same rule the write path enforces, applied to the view so a
      // re-upload of a period cannot appear twice in the table either.
      setRows((prev) => {
        const byPeriod = new Map(prev.map((r) => [r.period, r]));
        for (const s of result.snapshots) byPeriod.set(s.period, s);
        return [...byPeriod.values()].sort((a, b) => (a.period < b.period ? 1 : -1));
      });
      const periods = result.snapshots.map((s) => s.period).join(", ");
      setStatus({
        tone: "ok",
        text:
          result.note ??
          `${result.acceptedRows} row(s) accepted for ${periods}. Re-uploading a period replaces it.`,
      });
    });
  };

  const onDelete = (period: string) => {
    setStatus(null);
    setRowErrors([]);
    startTransition(async () => {
      const result = await deleteRevenueUploadAction(period);
      if (!result.ok) {
        setStatus({ tone: "err", text: result.error });
        return;
      }
      setRows((prev) => prev.filter((r) => r.period !== period));
      setStatus({ tone: "ok", text: `Removed ${period}.` });
    });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-xs text-muted">
          For revenue that lives in Chargebee, Recurly, Zuora, NetSuite or a spreadsheet. Upload{" "}
          <code className="rounded bg-ink/60 px-1">account_id, period, amount, currency</code> and
          it lands in the same events every other revenue source produces, so the margin column
          reads it identically. A period is a calendar month; re-uploading one replaces it.
        </p>
        {freshness.asOf && freshness.age ? (
          // Date only: a monthly snapshot's time of day is noise, and the relative age next to it
          // is what actually tells someone whether to act.
          <StaleBadge
            asOf={freshness.asOf.slice(0, 10)}
            age={freshness.age}
            stale={freshness.stale}
            verb="uploaded"
          />
        ) : null}
      </div>

      {freshness.stale && freshness.reason ? (
        <div className="rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-sm text-warn">
          <span className="font-medium">Snapshot is out of date. </span>
          <span>{freshness.reason}</span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <label
          className={`inline-flex cursor-pointer items-center rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25 ${
            pending ? "pointer-events-none opacity-50" : ""
          }`}
        >
          {pending ? "Uploading…" : "Upload revenue CSV"}
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onFile(file);
            }}
          />
        </label>
        <a
          href="/api/revenue-uploads/template"
          className="text-xs text-muted underline hover:text-accent"
        >
          Download the template
        </a>
      </div>

      {status && (
        <p className={`text-xs ${status.tone === "ok" ? "text-good" : "text-warn"}`}>
          {status.text}
        </p>
      )}

      {rowErrors.length > 0 && (
        <div className="rounded-lg border border-warn/40 bg-warn/5 p-3">
          <p className="text-xs font-medium text-warn">
            Nothing was written. Fix these lines and upload again:
          </p>
          <ul className="mt-2 space-y-1 text-xs text-muted">
            {rowErrors.slice(0, 25).map((e, i) => (
              <li key={`${e.line}-${i}`}>
                <span className="font-medium tabular-nums text-warn">line {e.line}</span>:{" "}
                {e.message}
              </li>
            ))}
            {rowErrors.length > 25 && (
              <li className="text-muted">…and {rowErrors.length - 25} more.</li>
            )}
          </ul>
        </div>
      )}

      {unreachable ? (
        <p className="text-xs text-muted">
          The gateway is unreachable, so we cannot say what has been uploaded. This is a blank, not
          a zero.
        </p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted">
          No revenue has been uploaded yet, so the margin column has no revenue side for accounts
          that are not covered by a connector.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Period</th>
                <th className="py-1 text-right font-medium">Accounts</th>
                <th className="py-1 text-right font-medium">Revenue</th>
                <th className="py-1 text-right font-medium">Uploaded</th>
                <th className="py-1 text-left font-medium">File</th>
                <th className="py-1 text-right font-medium" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.period} className="border-t border-edge">
                  <td className="py-2 font-medium tabular-nums">{r.period}</td>
                  <td className="py-2 text-right tabular-nums">
                    {r.accountCount.toLocaleString()}
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {formatSnapshotAmount(r.totalAmountMicro, r.currency)}
                  </td>
                  <td className="py-2 text-right tabular-nums text-muted">
                    {r.uploadedAt.slice(0, 10)}
                  </td>
                  <td className="py-2 text-muted">{r.filename ?? "—"}</td>
                  <td className="py-2 text-right">
                    <button
                      type="button"
                      onClick={() => onDelete(r.period)}
                      disabled={pending}
                      className="rounded-full border border-edge bg-ink/40 px-2.5 py-1 text-xs font-medium text-muted transition hover:bg-ink/60 disabled:opacity-50"
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
