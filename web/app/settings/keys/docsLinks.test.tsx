// SPDX-License-Identifier: Apache-2.0
// CTO-379: the connect panel and the proxy switch send people to the docs page for what they are
// setting up, and never to a page the docs site does not publish.
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DOCS_LINKS } from "@/lib/docsLinks";
import { ConnectPanel } from "./ConnectPanel";
import { ProxySwitch } from "./ProxySwitch";

function stubFirstEventPoll() {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "waiting" }) }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function learnMore(): HTMLAnchorElement {
  return screen.getByRole("link", { name: "Learn more" }) as HTMLAnchorElement;
}

describe("dashboard docs links", () => {
  it("connect panel links the Python SDK page when only the SDK path exists", () => {
    stubFirstEventPoll();
    render(
      <ConnectPanel
        token="tally_sk_live_test"
        endpoints={{ openaiProxyBaseUrl: "", anthropicProxyBaseUrl: "", sdkEndpoint: "", proxyDeployed: false }}
      />,
    );
    expect(learnMore().getAttribute("href")).toBe(DOCS_LINKS.pythonSdk);
    expect(learnMore().getAttribute("target")).toBe("_blank");
  });

  it("connect panel links the proxy page while the proxy path is showing", () => {
    stubFirstEventPoll();
    render(
      <ConnectPanel
        token="tally_sk_live_test"
        endpoints={{
          openaiProxyBaseUrl: "https://ingest.example.test/openai/v1",
          anthropicProxyBaseUrl: "https://ingest.example.test/anthropic",
          sdkEndpoint: "",
          proxyDeployed: true,
        }}
      />,
    );
    expect(learnMore().getAttribute("href")).toBe(DOCS_LINKS.proxy);
  });

  it("proxy switch links the proxy page", () => {
    render(<ProxySwitch initialEnabled={false} canManage={false} />);
    expect(learnMore().getAttribute("href")).toBe(DOCS_LINKS.proxy);
    expect(learnMore().getAttribute("rel")).toContain("noopener");
  });
});
