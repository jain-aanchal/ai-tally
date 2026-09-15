// SPDX-License-Identifier: Apache-2.0
// Links from the dashboard into the developer docs (CTO-379).
//
// The docs are a separate site (ai-tally-website, served at ai-tally.com/docs), so every link is an
// absolute URL built from ONE base: moving the docs is a one-line change here rather than a hunt
// through components. Each entry names a page that is actually published there; docsLinks.test.ts
// pins the list so a link to a page that does not exist cannot be added by accident.
export const DOCS_BASE_URL = "https://ai-tally.com/docs";

export const DOCS_LINKS = {
  home: DOCS_BASE_URL,
  quickstart: `${DOCS_BASE_URL}/get-started/quickstart`,
  checkItWorks: `${DOCS_BASE_URL}/get-started/check-it-works`,
  proxy: `${DOCS_BASE_URL}/connect/proxy`,
  pythonSdk: `${DOCS_BASE_URL}/connect/python-sdk`,
  opentelemetry: `${DOCS_BASE_URL}/connect/opentelemetry`,
  revenueUpload: `${DOCS_BASE_URL}/connect/revenue-upload`,
  security: `${DOCS_BASE_URL}/security`,
} as const;
