// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";
import type { ReactNode } from "react";

const NAV: { label: string; href: string }[] = [
  { label: "Get started", href: "/onboarding" },
  { label: "Home", href: "/" },
  { label: "Cost", href: "/cost" },
  { label: "Features", href: "/features" },
  { label: "Agents", href: "/agents" },
  { label: "Compare", href: "/compare" },
  { label: "Attribution", href: "/attribution" },
  { label: "Estimate", href: "/estimate" },
  { label: "Connectors", href: "/connectors" },
  { label: "Settings", href: "/settings" },
  { label: "Data Quality", href: "/data-quality" },
];

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-edge bg-panel px-3 py-4">
        <div className="px-2 pb-5 text-lg font-semibold tracking-tight">
          ai-<span className="text-accent">tally</span>
        </div>
        <nav className="space-y-0.5">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="block rounded-md px-3 py-2 text-sm text-gray-300 hover:bg-edge hover:text-white"
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-edge bg-panel/60 px-6 py-3 text-sm">
          <TopBarSelect label="Tenant" value="acme-prod" />
          <TopBarSelect label="Range" value="Last 30 days" />
          <TopBarSelect label="Feature" value="All features" />
          <TopBarSelect label="Env" value="production" />
        </header>
        <main className="min-w-0 flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}

function TopBarSelect({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs uppercase tracking-wide text-muted">{label}</span>
      <span className="rounded-md border border-edge bg-ink px-2 py-1 text-gray-200">{value}</span>
    </div>
  );
}
