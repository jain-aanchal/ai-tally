// SPDX-License-Identifier: Apache-2.0
// The recovery screen for the provisioning race (#358).
//
// What it replaces: a brand-new customer whose browser beat Clerk's `organization.created` webhook
// got Next's generic "Application error: a server-side exception has occurred" with an opaque
// digest, as their first screen, at the highest-stakes moment in the funnel. The throw was correct.
// Nothing catching it was not.
//
// The screen is deliberately NOT an infinite spinner. The race and a genuine provisioning failure
// look identical from the browser, and a customer parked forever on "setting up your workspace…"
// while the HMAC key provider returns 503 is the worse of the two failures, because it never tells
// them anything is wrong. So:
//
//   - We poll, bounded. Inside the window the wording is "still being set up", which is true.
//   - The moment the poll answers `failed` (any resolution failure that is not the deliberate 404)
//     we stop and say provisioning did not complete, with the reason. That is the distinguishing
//     signal: 404 means "not yet", anything else means "not working".
//   - When the window runs out with the answer still `pending`, we stop claiming it is nearly done.
//     A wait this long is no longer the race we can point at, so the screen says provisioning has
//     not completed, what we do know, and what to do next.

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { ProvisioningState, ProvisioningStatusPayload } from "@/lib/provisioning";

/** How often we re-ask. The race resolves in seconds; a 2s poll is a handful of requests. */
export const PROVISIONING_POLL_MS = 2_000;

/**
 * How long "still being set up" stays true.
 *
 * There is no measurement behind this number: Clerk webhook delivery latency against a page load
 * needs a deployed environment to observe, and the audit records that nobody has. It is a bound on
 * how long we are willing to imply things are fine, not a claim about how long delivery takes, and
 * the copy past it says exactly that.
 */
export const PROVISIONING_WAIT_MS = 45_000;

interface Screen {
  heading: string;
  body: string;
  /** Whether the customer is still being asked to wait (renders the progress affordance). */
  waiting: boolean;
}

/**
 * The pure state-to-copy mapping, so what the customer is told is unit-tested rather than tangled
 * up in the poll. `reason` is the server's explanation and is rendered verbatim when present.
 */
export function provisioningScreen(
  state: ProvisioningState,
  elapsedMs: number,
  reason: string | null,
): Screen {
  if (state === "failed") {
    const why = reason ?? "the control plane did not answer";
    return {
      heading: "Your workspace could not be set up",
      body:
        why.charAt(0).toUpperCase() +
        why.slice(1) +
        ". This will not clear on its own. Reload to try again, and if it persists, contact support with the name of the organization you just created.",
      waiting: false,
    };
  }
  if (elapsedMs >= PROVISIONING_WAIT_MS) {
    return {
      heading: "Your workspace has not finished being set up",
      body:
        "We have been waiting " +
        Math.round(elapsedMs / 1000) +
        " seconds and this organization still has no workspace behind it. That is longer than setup should take, so we have stopped implying it is about to finish. Reload to check again, and if it persists, contact support with the name of the organization you just created.",
      waiting: false,
    };
  }
  return {
    heading: "Setting up your workspace",
    body:
      "Your organization was created and we are waiting for its workspace to appear. This normally takes a few seconds and the page continues on its own.",
    waiting: true,
  };
}

export function WorkspaceProvisioning({
  /** Test seam: the poll interval and the wait bound, so a test does not sit through 45 seconds. */
  pollMs = PROVISIONING_POLL_MS,
}: {
  pollMs?: number;
}) {
  const [state, setState] = useState<ProvisioningState>("pending");
  const [reason, setReason] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const startedAt = useRef(Date.now());
  const stopped = useRef(false);

  const poll = useCallback(async () => {
    try {
      const res = await fetch("/api/tenant/provisioning-status", { cache: "no-store" });
      const body = (await res.json()) as ProvisioningStatusPayload;
      if (stopped.current) return;
      if (body.state === "ready") {
        stopped.current = true;
        // A full navigation, not router.refresh(): the tenant resolves in the root layout, and a
        // hard load is the one thing guaranteed to re-run it with the cache cold.
        window.location.reload();
        return;
      }
      if (body.state === "no_org") {
        // NoActiveOrgError's own docstring: callers redirect to select-or-create-org.
        stopped.current = true;
        window.location.href = "/select-org";
        return;
      }
      setState(body.state);
      setReason(body.reason);
      if (body.state === "failed") stopped.current = true;
    } catch {
      // A dropped request is not evidence that provisioning failed, so it changes nothing: the next
      // tick asks again, and the elapsed bound below still applies.
    }
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      setElapsedMs(Date.now() - startedAt.current);
      if (stopped.current) return;
      if (Date.now() - startedAt.current >= PROVISIONING_WAIT_MS) return;
      void poll();
    }, pollMs);
    void poll();
    return () => clearInterval(id);
  }, [poll, pollMs]);

  const screen = provisioningScreen(state, elapsedMs, reason);

  return (
    <div className="flex min-h-[70vh] items-center justify-center p-6">
      <section
        className="max-w-lg rounded-xl border border-edge bg-panel p-6"
        aria-live="polite"
        role="status"
      >
        <h1 className="text-base font-semibold text-fg">{screen.heading}</h1>
        <p className="mt-2 text-sm text-muted">{screen.body}</p>
        {screen.waiting ? (
          <div className="mt-4 flex items-center gap-2 text-xs text-muted">
            <span
              aria-hidden
              className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent"
            />
            Checking every {Math.round(pollMs / 1000)}s
          </div>
        ) : (
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-4 inline-flex items-center rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25"
          >
            Reload
          </button>
        )}
      </section>
    </div>
  );
}
