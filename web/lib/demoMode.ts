// SPDX-License-Identifier: Apache-2.0
// Demo presentation flag. When NEXT_PUBLIC_DEMO_MODE is "1" the dashboard hides explanatory prose the
// product normally shows for honesty-under-uncertainty (forecast caveats, data-quality warnings,
// replay-diagnostic captions, empty-state "why this page is blank" explainers), leaving numbers,
// charts and tables. PRESENTATION only: nothing computed or stored changes. Defaults OFF, so the full
// copy renders unless a demo build opts in, and every existing test (which never sets the var) keeps
// asserting that copy. NEXT_PUBLIC_ prefix so it resolves identically in server and client components.
export function isDemoMode(): boolean {
  return process.env.NEXT_PUBLIC_DEMO_MODE === "1";
}
