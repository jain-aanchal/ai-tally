// SPDX-License-Identifier: Apache-2.0
// CTO-381: the connector form must ask for the reference shapes the gateway can resolve, and the
// trust policy it shows must pin sts:ExternalId to the organization id.
import { describe, expect, it } from "vitest";

import {
  AWS_ACCOUNT_PLACEHOLDER,
  credentialHelp,
  credentialKind,
  trustPolicy,
} from "./connectorCredentials";

const ORG = "11111111-2222-4333-8444-555555555555";

describe("credentialKind", () => {
  it("maps every configurable connector to the reference shape the gateway resolves", () => {
    expect(credentialKind("aws_cost_explorer")).toBe("aws_role");
    expect(credentialKind("aws_egress")).toBe("aws_role");
    expect(credentialKind("vercel")).toBe("token_secret");
    expect(credentialKind("vercel_egress")).toBe("token_secret");
    expect(credentialKind("cloudflare")).toBe("token_secret");
    expect(credentialKind("gcp_billing")).toBe("gcp_unsupported_hosted");
    expect(credentialKind("stripe")).toBeNull();
  });
});

describe("trustPolicy", () => {
  it("pins the external id to the organization id and trusts the configured account", () => {
    const policy = JSON.parse(trustPolicy("123456789012", ORG));
    const stmt = policy.Statement[0];
    expect(stmt.Action).toBe("sts:AssumeRole");
    expect(stmt.Principal.AWS).toBe("arn:aws:iam::123456789012:root");
    expect(stmt.Condition.StringEquals["sts:ExternalId"]).toBe(ORG);
  });

  it("uses a visible placeholder rather than inventing an account id", () => {
    for (const account of [null, "", "not-an-account"]) {
      const policy = JSON.parse(trustPolicy(account, ORG));
      expect(policy.Statement[0].Principal.AWS).toContain(AWS_ACCOUNT_PLACEHOLDER);
    }
  });
});

describe("credentialHelp", () => {
  it("asks for a role ARN with the external id, and a Secrets Manager ARN for tokens", () => {
    expect(credentialHelp("aws_cost_explorer")).toMatch(/IAM role ARN/);
    expect(credentialHelp("aws_cost_explorer")).toMatch(/sts:ExternalId/);
    expect(credentialHelp("vercel")).toMatch(/Secrets Manager secret ARN/);
    expect(credentialHelp("gcp_billing")).toMatch(/Not supported for hosted organizations yet/);
  });

  it("never suggests the ambient credential chain", () => {
    for (const c of ["aws_cost_explorer", "aws_egress", "vercel", "cloudflare", "gcp_billing"]) {
      expect(credentialHelp(c) ?? "").not.toContain("aws-default-chain");
    }
  });
});
