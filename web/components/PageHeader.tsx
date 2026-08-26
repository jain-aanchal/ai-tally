// SPDX-License-Identifier: Apache-2.0
// Shared page header + toolbar wrapper for the visual refresh (CTO-221, D1). Pages adopt it later;
// nothing is restyled here. It standardizes the top-of-page layout every workflow hand-rolls today
// (a title row on the left, live/status badges on the right, a filter toolbar underneath) so the
// overhaul lands one consistent header instead of ten slightly different ones.

import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  /** A short line under the title (what this page measures, the window, etc.). */
  subtitle?: ReactNode;
  /** Right-aligned slot: LiveIndicator, stale badges, export buttons. */
  actions?: ReactNode;
  /** Full-width slot under the title row, for the FilterBar / a toolbar. */
  toolbar?: ReactNode;
}

export function PageHeader({ title, subtitle, actions, toolbar }: PageHeaderProps) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">{title}</h1>
          {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
      {toolbar}
    </div>
  );
}
