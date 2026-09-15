// SPDX-License-Identifier: Apache-2.0
// Docs export of the connect snippets (CTO-371). The docs site at ai-tally.com/docs shows the exact
// strings the dashboard's connect panel shows, generated from connectSnippets.ts rather than retyped,
// so a changed header or base URL cannot leave the docs describing a connection that no longer works.
//
// Two things differ from the dashboard's one-time view, both on purpose:
//   * The key is a placeholder. The dashboard inlines a freshly minted key; a published page must
//     never carry one, so the docs get YOUR_TALLY_KEY and tell the reader to use their own.
//   * The endpoints are pinned to the hosted production ingest host instead of being read from this
//     deployment's environment, because the docs describe the hosted product, not whatever
//     environment happened to run the export.
//
// connectSnippetsExport.test.ts compares this output to the committed docs/public-api copy and fails
// when it is stale; UPDATE_DOCS_ARTIFACTS=1 rewrites it.
import { connectSnippets, defaultEndpoints, type ConnectEndpoints, type ConnectPath, type Snippet } from "./connectSnippets";

export const DOCS_PLACEHOLDER_KEY = "YOUR_TALLY_KEY";
export const HOSTED_INGEST_URL = "https://ingest.ai-tally.com";

export interface ConnectSnippetsExport {
  generated_from: string;
  placeholder_key: string;
  endpoints: ConnectEndpoints;
  snippets: Record<ConnectPath, Snippet[]>;
}

export function connectSnippetsForDocs(): ConnectSnippetsExport {
  // Only TALLY_INGEST_URL is set, which is how the hosted deployment derives both proxy base URLs.
  // sdkEndpoint stays empty, so the SDK snippet relies on the SDK's own default endpoint, which is
  // the same hosted ingest host.
  const endpoints = defaultEndpoints({ TALLY_INGEST_URL: HOSTED_INGEST_URL });
  return {
    generated_from: "web/lib/connectSnippets.ts",
    placeholder_key: DOCS_PLACEHOLDER_KEY,
    endpoints,
    snippets: connectSnippets(DOCS_PLACEHOLDER_KEY, endpoints),
  };
}

export function renderConnectSnippetsExport(): string {
  return `${JSON.stringify(connectSnippetsForDocs(), null, 2)}\n`;
}
