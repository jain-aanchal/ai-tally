// SPDX-License-Identifier: Apache-2.0
// CTO-381: the connect form shows the organization id (the sts:ExternalId the gateway sends) and,
// for AWS, the trust policy the customer's role needs. Before this the id was shown nowhere.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("./costConnectorActions", () => ({
  connectCostConnectorAction: vi.fn(),
  disconnectCostConnectorAction: vi.fn(),
}));

import { ConnectForm } from "./ConnectForm";

const ORG = "11111111-2222-4333-8444-555555555555";

function open(connector: string, props: { organizationId?: string | null; awsAccountId?: string | null }) {
  render(<ConnectForm connector={connector} configured={false} credentialsRef={null} details={{}} {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
}

describe("ConnectForm credential setup (CTO-381)", () => {
  it("shows the organization id and a trust policy pinned to it for AWS", () => {
    open("aws_cost_explorer", { organizationId: ORG, awsAccountId: "123456789012" });
    expect(screen.getByText(ORG)).toBeTruthy();
    const policy = JSON.parse(screen.getByTestId("trust-policy").textContent ?? "{}");
    expect(policy.Statement[0].Condition.StringEquals["sts:ExternalId"]).toBe(ORG);
    expect(policy.Statement[0].Principal.AWS).toBe("arn:aws:iam::123456789012:root");
    expect(screen.getByText("IAM role ARN")).toBeTruthy();
  });

  it("asks for a Secrets Manager ARN for token connectors and shows no trust policy", () => {
    open("vercel", { organizationId: ORG });
    expect(screen.getByText(ORG)).toBeTruthy();
    expect(screen.queryByTestId("trust-policy")).toBeNull();
    expect(screen.getByText(/Secrets Manager secret ARN holding the token/)).toBeTruthy();
  });

  it("renders an honest blank rather than a made-up id when the organization is unknown", () => {
    open("aws_egress", { organizationId: null });
    expect(screen.queryByTestId("trust-policy")).toBeNull();
    expect(screen.getByText(/Organization id/)).toBeTruthy();
  });
});
