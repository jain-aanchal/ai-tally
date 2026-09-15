// SPDX-License-Identifier: Apache-2.0
// CTO-379: dashboard docs links must stay under the one base URL and point only at pages the docs
// site publishes. When a docs page is added or renamed, update this list and docsLinks.ts together.
import { describe, expect, it } from "vitest";

import { DOCS_BASE_URL, DOCS_LINKS } from "./docsLinks";

const PUBLISHED_PAGES = [
  "",
  "/connect/opentelemetry",
  "/connect/proxy",
  "/connect/python-sdk",
  "/connect/revenue-upload",
  "/get-started/check-it-works",
  "/get-started/quickstart",
  "/security",
];

describe("DOCS_LINKS", () => {
  it("builds every link from the single docs base URL", () => {
    for (const url of Object.values(DOCS_LINKS)) {
      expect(url.startsWith(DOCS_BASE_URL)).toBe(true);
    }
  });

  it("points only at pages the docs site publishes", () => {
    const paths = Object.values(DOCS_LINKS).map((url) => url.slice(DOCS_BASE_URL.length));
    for (const path of paths) {
      expect(PUBLISHED_PAGES).toContain(path);
    }
  });

  it("uses slash-free URLs, matching the docs site", () => {
    for (const url of Object.values(DOCS_LINKS)) {
      expect(url.endsWith("/")).toBe(false);
    }
  });
});
