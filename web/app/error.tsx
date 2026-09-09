// SPDX-License-Identifier: Apache-2.0
// The app's error boundary (#358). There was none anywhere under web/app, so every uncaught render
// error, including the provisioning race, rendered Next's built-in "Application error: a
// server-side exception has occurred (see more info in server logs)" with a digest and nothing else.
//
// Two rules here, both from the audit:
//
//   1. The digest is not the message. In a production build Next redacts a server-side error before
//      it reaches this component, so the digest is genuinely all we have; that makes it a reference
//      to quote to support, not an explanation. It renders as small print under a sentence that
//      says what actually happened in the customer's terms, and it is never the headline.
//   2. No stack. `error.stack` is not rendered, in any environment. What a developer needs is in
//      the server log, which the copy points at.
//
// The provisioning race does not arrive here: the root layout resolves the tenant and renders the
// recovery screen for it, because that error is transient and recoverable and this one is neither.

"use client";

import Link from "next/link";
import { useEffect } from "react";

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // The server log is where the un-redacted error lives; this puts the client half next to it.
    console.error("unhandled app error", error.digest ?? "");
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <section className="max-w-lg rounded-xl border border-edge bg-panel p-6">
        <h1 className="text-base font-semibold text-fg">This page could not be loaded</h1>
        <p className="mt-2 text-sm text-muted">
          Something went wrong while building this view. Nothing was changed, and your data is
          unaffected. Try again, and if it keeps happening, contact support with the reference below.
        </p>
        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            onClick={reset}
            className="inline-flex items-center rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25"
          >
            Try again
          </button>
          <Link href="/" className="text-sm text-muted underline hover:text-fg">
            Back to Home
          </Link>
        </div>
        {error.digest ? (
          <p className="mt-4 text-xs text-muted">
            Reference: <code className="font-mono">{error.digest}</code>
          </p>
        ) : null}
      </section>
    </div>
  );
}
