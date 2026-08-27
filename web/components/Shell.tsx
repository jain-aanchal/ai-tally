// SPDX-License-Identifier: Apache-2.0
// The global app shell: sidebar nav + main content frame (CTO-225, D5, building on the CTO-221
// design foundation). This is DESIGN only. Every route and every nav link is preserved exactly;
// the refresh adds grouping captions, active-route highlighting, and token-driven spacing so the
// app reads as one coherent surface instead of a flat list of links.
//
// It is a client component so it can highlight the active route via `usePathname`. Children are
// still server-rendered in layout.tsx and passed through as an opaque node, so no page loses its
// server rendering by living under this shell.

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

interface NavItem {
  label: string;
  href: string;
}

// Links are grouped only for visual grouping; the set and order of routes is unchanged from the
// flat list this shell shipped before. Budgets stays linked directly (real tenant-declared config
// backed by the gateway control plane, CTO-208 F4) rather than hidden behind /settings.
//
// Hidden from the nav until they have real signal end-to-end (pages still render at the URL):
//   - /settings        — guardrail config only, nothing else wired
//   - /estimate        — mock fixtures (re-add when CTO-128 lands)
//   - /data-quality    — placeholder rows (re-add when DQ follow-ups land)
const NAV_GROUPS: { caption: string; items: NavItem[] }[] = [
  {
    caption: "Overview",
    items: [{ label: "Home", href: "/" }],
  },
  {
    caption: "Analyze",
    items: [
      { label: "Cost", href: "/cost" },
      { label: "Features", href: "/features" },
      { label: "Agents", href: "/agents" },
      { label: "Compare", href: "/compare" },
      { label: "Attribution", href: "/attribution" },
      { label: "Unit Economics", href: "/unit-economics" },
      { label: "Cost per Customer", href: "/cost-per-customer" },
    ],
  },
  {
    caption: "Configure",
    items: [
      { label: "Connectors", href: "/connectors" },
      { label: "Guardrails", href: "/guardrails" },
      { label: "Budgets", href: "/settings/budgets" },
    ],
  },
];

// Active when the path is the link or nested under it. "/" matches only itself so it does not light
// up for every route; the `href + "/"` guard keeps "/cost" from claiming "/cost-per-customer".
function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/";

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-edge bg-panel">
        <div className="flex items-center gap-2 px-5 py-5 text-lg font-semibold tracking-tight">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm bg-accent"
          />
          {/* The wordmark is one flex child so the row `gap-2` only separates it from the mark, not
              the two halves of "ai-tally"; without this wrapper the gap split it into "ai- tally"
              with a dangling hyphen (CTO-227). */}
          <span>
            ai-<span className="text-accent">tally</span>
          </span>
        </div>

        <nav className="app-scroll flex-1 space-y-6 overflow-y-auto px-3 pb-6" aria-label="Primary">
          {NAV_GROUPS.map((group) => (
            <div key={group.caption} className="space-y-1">
              <div className="px-3 text-[11px] font-medium uppercase tracking-wider text-muted">
                {group.caption}
              </div>
              <div className="space-y-0.5">
                {group.items.map((item) => {
                  const active = isActive(pathname, item.href);
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      aria-current={active ? "page" : undefined}
                      className={[
                        "relative block rounded-md px-3 py-2 text-sm transition-colors",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                        active
                          ? "bg-edge font-medium text-white"
                          : "text-gray-300 hover:bg-edge/60 hover:text-white",
                      ].join(" ")}
                    >
                      {active && (
                        <span
                          aria-hidden
                          className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-accent"
                        />
                      )}
                      {item.label}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-edge px-5 py-3 text-[11px] text-muted">
          Cost-and-value observability
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <main className="mx-auto w-full min-w-0 max-w-7xl flex-1 p-6 lg:p-8">{children}</main>
      </div>
    </div>
  );
}
