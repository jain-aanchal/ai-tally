// SPDX-License-Identifier: Apache-2.0
// Home's entry point into /onboarding (#358).
//
// The two-step setup experience, the coverage probe and the honest-blank work behind them existed
// with nothing linking to them: no href in the app, no nav entry, no mention in the docs. A page
// nobody can reach is the same as a page that does not exist.
//
// The callout is driven by the ONE signal the product already has for "has any data arrived":
// queryFirstEventSeen (a tenant-scoped existence probe). It has three states and all three are
// rendered differently, because collapsing them is how a product ends up telling a tenant with a
// broken ClickHouse read that they have not connected anything:
//
//   connected -> nothing at all. A tenant with data flowing does not need setup in their face.
//   waiting   -> the probe ran and found no span. That is a real, definite negative, so we say it
//                and point at setup.
//   unknown   -> the probe could not be read. We do NOT assume the tenant is new. The callout says
//                we cannot tell and still offers the link, which is the honest version of the same
//                affordance (CLAUDE.md, honest under uncertainty).
//
// The permanent way back in is the "Setup" nav item in the shell; this callout is the prominent
// one, and it disappears on its own once a span lands.

import Link from "next/link";

import type { FirstEventStatus } from "@/lib/firstEvent";

/** Copy for each state the callout renders. `null` for a tenant whose data is already flowing. */
export function setupCalloutCopy(
  status: FirstEventStatus,
): { heading: string; body: string; cta: string } | null {
  if (status === "connected") return null;
  if (status === "waiting") {
    return {
      heading: "No telemetry has arrived yet",
      body:
        "We checked and found no spans for this workspace. Connect your app and the dashboards below fill in.",
      cta: "Finish setup",
    };
  }
  return {
    heading: "We could not tell whether your telemetry has arrived",
    body:
      "The first-event probe could not be read, so this is not a report that nothing has arrived. " +
      "If the dashboards below are blank, setup is the place to start.",
    cta: "Open setup",
  };
}

export function SetupCallout({ status }: { status: FirstEventStatus }) {
  const copy = setupCalloutCopy(status);
  if (!copy) return null;
  return (
    <section
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-accent/40 bg-accent/10 px-4 py-3"
      aria-label="Setup"
    >
      <div className="min-w-0">
        <div className="text-sm font-medium text-fg">{copy.heading}</div>
        <p className="mt-0.5 text-sm text-muted">{copy.body}</p>
      </div>
      <Link
        href="/onboarding"
        className="inline-flex shrink-0 items-center rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25"
      >
        {copy.cta}
      </Link>
    </section>
  );
}
