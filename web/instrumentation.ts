// SPDX-License-Identifier: Apache-2.0
// Next.js instrumentation hook (CTO-268).
//
// WHY HERE. `register()` is the only place Next.js guarantees to run ONCE, before the server
// accepts its first request, in every way this app is started: `next start`, the standalone
// `node server.js` both Dockerfiles ship, `next dev`, and once per runtime (Node and Edge). That is
// exactly what a security assertion needs. The alternatives all fail the requirement:
//
//   * A check inside a route handler or a server component fires on the FIRST REQUEST to that one
//     page, and any route that forgets to call it is unguarded.
//   * `middleware.ts` module scope is evaluated lazily by the Edge runtime, so a misconfigured
//     server still comes up and only fails once traffic arrives. Worse, `process.env` reads in the
//     Edge bundle can be inlined at BUILD time, so a copy of this check there would be judging the
//     build's environment rather than the container's.
//   * `app/layout.tsx` module scope runs at render time, and it is prerendered at build time, so it
//     says nothing about the environment the container is finally started with.
//
// The guard is deliberately fatal rather than throw-and-hope: Next logs an instrumentation error but
// its behaviour on a throw is not a contract we want a security control to rest on, so we print the
// message and exit non-zero ourselves. An orchestrator (ECS, Cloud Run, Kubernetes, Compose) then
// shows a crash-looping task with the reason in the logs, which is what an operator mid-deploy
// needs to see.

import { assertAuthConfig } from "./lib/authGuard";

export function register(): void {
  try {
    assertAuthConfig(process.env);
  } catch (err) {
    console.error(err instanceof Error ? err.message : String(err));
    // The Edge runtime has no process.exit; there a throw is all we have (and the Node runtime's
    // register() has already exited the process by then anyway).
    if (typeof process !== "undefined" && typeof process.exit === "function") {
      process.exit(1);
    }
    throw err;
  }
}
