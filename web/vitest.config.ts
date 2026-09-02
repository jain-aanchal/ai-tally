// SPDX-License-Identifier: Apache-2.0
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": resolve(__dirname, ".") },
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
