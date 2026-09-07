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
    // CI run the product with no Clerk keys.
    //
    // The pinned value is a UUID, not the name `local-dev`, because the canonical TenantId is the
    // tenant UUID (§8) and this value is bound straight into the ClickHouse read filter. Pinning a
    // name here would let a read path that only works for names pass in CI and then render an empty
    // dashboard against real data. The all-zero UUID is deliberately not a real tenant; it matches
    // the placeholder in web/.env.example.
    env: { TALLY_DEV_TENANT: "00000000-0000-0000-0000-000000000000" },
  },
});
