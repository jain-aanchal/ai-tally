// SPDX-License-Identifier: Apache-2.0
// Last-resort boundary (#358). `app/error.tsx` sits INSIDE the root layout, so it cannot catch an
// error thrown by the layout itself: that is what this file is for, and it has to render its own
// <html>/<body> because the layout that would have provided them is the thing that failed.
//
// It is deliberately plain and dependency-free (inline styles, no shell, no Clerk): every richer
// thing it could reach for is a thing that might be what broke. Same two rules as error.tsx, the
// digest is a reference and not the message, and no stack is rendered.

"use client";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body style={{ margin: 0, background: "#0b0d10", color: "#e6e8eb", fontFamily: "system-ui, sans-serif" }}>
        <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
          <section style={{ maxWidth: 520, border: "1px solid #23282e", borderRadius: 12, padding: 24 }}>
            <h1 style={{ fontSize: 16, margin: 0 }}>ai-tally could not start this page</h1>
            <p style={{ fontSize: 14, lineHeight: 1.5, color: "#9aa3ad" }}>
              The application shell failed to load. This is on our side, not something you did, and
              no data was changed. Try again, and if it keeps happening, contact support with the
              reference below.
            </p>
            <button
              type="button"
              onClick={reset}
              style={{
                marginTop: 8,
                padding: "6px 12px",
                fontSize: 14,
                borderRadius: 6,
                border: "1px solid #3d6fe0",
                background: "transparent",
                color: "#7aa2f7",
                cursor: "pointer",
              }}
            >
              Try again
            </button>
            {error.digest ? (
              <p style={{ fontSize: 12, color: "#6b7480" }}>Reference: {error.digest}</p>
            ) : null}
          </section>
        </div>
      </body>
    </html>
  );
}
