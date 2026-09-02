// SPDX-License-Identifier: Apache-2.0
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "."),
      // `getTenant.ts` imports `server-only` (CTO-259), whose default export throws by design so a
      // client bundle cannot pull in a server module. Vitest runs in Node, not the RSC/client
      // bundler, and legitimately imports server modules under test (getTenant, route handlers), so
      // point the marker at its empty build to make it a no-op here. This is exactly the shim Next's
      // bundler applies via the `react-server` export condition.
      "server-only": resolve(__dirname, "node_modules/server-only/empty.js"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    // The dev escape hatch (Initiative 1, §10). Tests run with no Clerk account, so `getTenant()`
    // short-circuits to a pinned tenant instead of consulting Clerk. This mirrors how `make up` and
    // CI run the product with no Clerk keys, and keeps the tenant scoping in the fetch-shaped tests
    // exactly what it was before Clerk (`local-dev`).
    env: { TALLY_DEV_TENANT: "local-dev" },
  },
});
