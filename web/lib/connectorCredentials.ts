// SPDX-License-Identifier: Apache-2.0
// What each cloud cost connector's credential field must hold, and the IAM trust policy a customer
// adds so ai-tally can act as them (CTO-381).
//
// Before CTO-381 the gateway never resolved these references: AWS jobs ran as ai-tally's own
// identity and Vercel/Cloudflare calls went out unauthenticated. The gateway now assumes the
// customer's IAM role with their organization id as sts:ExternalId, and reads API tokens from AWS
// Secrets Manager. The form has to ask for exactly those shapes, so the rules live here as pure,
// client-safe helpers that the form renders and the tests pin.

export type CredentialKind = "aws_role" | "token_secret" | "gcp_unsupported_hosted";

const KIND: Record<string, CredentialKind> = {
  aws_cost_explorer: "aws_role",
  aws_egress: "aws_role",
  vercel: "token_secret",
  vercel_egress: "token_secret",
  cloudflare: "token_secret",
  gcp_billing: "gcp_unsupported_hosted",
};

export function credentialKind(connector: string): CredentialKind | null {
  return KIND[connector] ?? null;
}

/** Shown in place of ai-tally's AWS account id when the deployment has not configured it. */
export const AWS_ACCOUNT_PLACEHOLDER = "<ai-tally AWS account id>";

/**
 * The trust policy the customer attaches to the role they connect. The principal is ai-tally's
 * AWS account and the condition pins sts:ExternalId to their organization id, which is what stops
 * another organization from pointing ai-tally at the same role.
 */
export function trustPolicy(awsAccountId: string | null, organizationId: string): string {
  const account = awsAccountId && /^\d{12}$/.test(awsAccountId) ? awsAccountId : AWS_ACCOUNT_PLACEHOLDER;
  return JSON.stringify(
    {
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: { AWS: `arn:aws:iam::${account}:root` },
          Action: "sts:AssumeRole",
          Condition: { StringEquals: { "sts:ExternalId": organizationId } },
        },
      ],
    },
    null,
    2,
  );
}

/** One line of guidance under the credential field, by connector. */
export function credentialHelp(connector: string): string | null {
  switch (credentialKind(connector)) {
    case "aws_role":
      return (
        "An IAM role ARN in your account. Its trust policy must allow ai-tally's AWS account with " +
        "your organization id as sts:ExternalId (shown below)."
      );
    case "token_secret":
      return (
        "An AWS Secrets Manager secret ARN holding the token. It is read through your connected AWS " +
        "role; without one, the secret must be named ai-tally/connectors/<organization id>/..."
      );
    case "gcp_unsupported_hosted":
      return "Not supported for hosted organizations yet. Only a self-hosted single-tenant deployment accepts it.";
    default:
      return null;
  }
}
