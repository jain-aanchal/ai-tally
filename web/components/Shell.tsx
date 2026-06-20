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
  { label: "Connectors", href: "/connectors" },
  { label: "Settings", href: "/settings" },
  // Estimate (/estimate) and Data Quality (/data-quality) hidden from the nav — the pages
  // still render at their URLs but most of their tiles are mock fixtures today. Re-add to NAV
  // when CTO-128 (estimate body-driven what-if) and the DQ follow-ups land.
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
        <main className="min-w-0 flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
