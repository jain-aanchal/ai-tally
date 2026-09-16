// SPDX-License-Identifier: Apache-2.0
"use client";

// Connect / edit / disconnect one cloud cost connector (CTO-176). Before this, the page could only
// say "configured in the backend connector runner"; config rows had to be inserted into Postgres
// by hand. The form posts through a server action to the gateway, which owns all field validation.
//
// Credentials are references, never raw keys. The gateway rejects anything shaped like an actual
// secret, so a pasted key fails at the edge instead of landing in a column. CTO-381: the gateway
// now resolves each reference as the organization (an IAM role assumed with the organization id as
// sts:ExternalId, or a Secrets Manager ARN), so the form asks for those shapes and shows the
// organization id and trust policy the customer needs to set that up.
import { useState, useTransition } from "react";

import { Blank } from "@/components/HonestValue";
import { credentialHelp, credentialKind, trustPolicy } from "@/lib/connectorCredentials";

import { connectCostConnectorAction, disconnectCostConnectorAction } from "./costConnectorActions";

interface FieldDef {
  name: string;
  label: string;
  placeholder?: string;
  hint?: string;
  required?: boolean;
  type?: "text" | "checkbox";
}

/** Per-connector fields. Mirrors the required-field checks in gateway config_admin. */
const FIELDS: Record<string, FieldDef[]> = {
  aws_cost_explorer: [
    {
      name: "credentials_ref",
      label: "IAM role ARN",
      placeholder: "arn:aws:iam::123456789012:role/ai-tally-connector-cost-reader",
      hint: credentialHelp("aws_cost_explorer") ?? undefined,
      required: true,
    },
    {
      name: "tag_filter",
      label: "Cost allocation tags (JSON)",
      placeholder: '{"tally:workload":"ai"}',
      hint: "Scopes the billing query to your AI workload. Leave blank for the default.",
    },
  ],
  gcp_billing: [
    {
      name: "credentials_ref",
      label: "Credential reference",
      placeholder: "projects/my-proj/secrets/tally-billing-sa",
      hint: credentialHelp("gcp_billing") ?? undefined,
      required: true,
    },
    {
      name: "bq_billing_export_table",
      label: "Billing export table",
      placeholder: "my-project.billing.gcp_billing_export_v1_XXXX",
      hint: "GCP has no fine-grained cost API, so the source of truth is the BigQuery billing export.",
      required: true,
    },
    {
      name: "label_filter",
      label: "Labels (JSON)",
      placeholder: '{"tally-workload":"ai"}',
      hint: "GCP label keys cannot contain ':', so this uses a '-' unlike the AWS tag filter.",
    },
  ],
  vercel: [
    {
      name: "access_token_ref",
      label: "Access token secret ARN",
      placeholder: "arn:aws:secretsmanager:us-east-1:123456789012:secret:vercel-token-AbCdEf",
      hint: credentialHelp("vercel") ?? undefined,
      required: true,
    },
    { name: "team_id", label: "Team id", placeholder: "team_xxx", hint: "Public identifier, not a secret." },
    { name: "project_id", label: "Project id", placeholder: "prj_xxx" },
    {
      name: "emit_egress",
      label: "Also emit Vercel egress",
      type: "checkbox",
      hint: "Leave off if you connect Vercel egress separately. Only one path may own egress.",
    },
  ],
  cloudflare: [
    {
      name: "credentials_ref",
      label: "API token secret ARN",
      placeholder: "arn:aws:secretsmanager:us-east-1:123456789012:secret:cloudflare-token-AbCdEf",
      hint: credentialHelp("cloudflare") ?? undefined,
      required: true,
    },
    { name: "resource_id", label: "Zone id", placeholder: "023e105f4ecef8ad9ca31a8372d0c353", required: true },
    {
      name: "usd_per_gb",
      label: "USD per GB",
      placeholder: "0.09",
      hint: "Required. Cloudflare reports bytes, not dollars, and we will not guess a price.",
      required: true,
    },
  ],
  aws_egress: [
    {
      name: "credentials_ref",
      label: "IAM role ARN",
      placeholder: "arn:aws:iam::123456789012:role/ai-tally-connector-cost-reader",
      hint: `${credentialHelp("aws_egress")} Same Cost Explorer access as compute, filtered to DataTransfer-Out-Bytes.`,
      required: true,
    },
    { name: "resource_id", label: "Account id", placeholder: "123456789012" },
  ],
  vercel_egress: [
    {
      name: "credentials_ref",
      label: "Access token secret ARN",
      placeholder: "arn:aws:secretsmanager:us-east-1:123456789012:secret:vercel-token-AbCdEf",
      hint: credentialHelp("vercel_egress") ?? undefined,
      required: true,
    },
    { name: "resource_id", label: "Team id", placeholder: "team_xxx" },
  ],
};

interface Props {
  connector: string;
  configured: boolean;
  credentialsRef: string | null;
  details: Record<string, unknown>;
  /** The tenant UUID, which is the sts:ExternalId the gateway sends. Null when it could not be resolved. */
  organizationId?: string | null;
  /** ai-tally's AWS account id for the trust policy principal. Null when the deployment has not set it. */
  awsAccountId?: string | null;
  /** CTO-392: false for a non-admin. The server actions behind Save / Disconnect refuse the write
   *  either way; this only stops the form offering them. */
  canEdit?: boolean;
}

/** CTO-381: the organization id and, for AWS, the trust policy the customer's role needs. */
function CredentialSetup({
  connector,
  organizationId,
  awsAccountId,
}: {
  connector: string;
  organizationId: string | null;
  awsAccountId: string | null;
}) {
  return (
    <div className="mb-3 space-y-2 text-[10px] leading-snug text-muted">
      <p>
        Organization id (your external id):{" "}
        {organizationId ? (
          <code className="select-all break-all text-fg">{organizationId}</code>
        ) : (
          <Blank reason="the organization id could not be resolved for this session" />
        )}
      </p>
      {credentialKind(connector) === "aws_role" && organizationId && (
        <div>
          <p>
            Trust policy for the role
            {awsAccountId ? "" : " (ask ai-tally for its AWS account id; it is not configured here)"}:
          </p>
          <pre
            data-testid="trust-policy"
            className="mt-1 max-h-40 overflow-auto rounded border border-edge bg-bg p-2 text-[10px]"
          >
            {trustPolicy(awsAccountId, organizationId)}
          </pre>
        </div>
      )}
    </div>
  );
}

function initialValues(connector: string, details: Record<string, unknown>, ref: string | null) {
  const vals: Record<string, string> = {};
  for (const f of FIELDS[connector] ?? []) {
    if (f.name === "credentials_ref" || f.name === "access_token_ref") {
      vals[f.name] = ref ?? "";
      continue;
    }
    const raw = details[f.name];
    if (raw === undefined || raw === null) {
      vals[f.name] = f.type === "checkbox" ? "false" : "";
    } else if (typeof raw === "object") {
      vals[f.name] = JSON.stringify(raw);
    } else {
      vals[f.name] = String(raw);
    }
  }
  return vals;
}

export function ConnectForm({
  connector,
  configured,
  credentialsRef,
  details,
  organizationId = null,
  awsAccountId = null,
  canEdit = true,
}: Props) {
  const fields = FIELDS[connector];
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState(() => initialValues(connector, details, credentialsRef));
  const [status, setStatus] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [pending, startTransition] = useTransition();

  if (!fields) return <span className="text-xs text-muted">—</span>;
  // A member sees the configured state, which is a read, and no way to change it.
  if (!canEdit) {
    return (
      <span className="text-xs text-muted" title="Admins only">
        {configured ? "Configured" : "Not configured"}
      </span>
    );
  }

  const submit = () => {
    setStatus(null);
    startTransition(async () => {
      const res = await connectCostConnectorAction(connector, values);
      if (res.ok) {
        setStatus({ tone: "ok", text: res.note ?? "Saved. The next connector run will use it." });
        setOpen(false);
      } else {
        setStatus({ tone: "err", text: res.error });
      }
    });
  };

  const disconnect = () => {
    setStatus(null);
    startTransition(async () => {
      const res = await disconnectCostConnectorAction(connector);
      if (res.ok) {
        setStatus({ tone: "ok", text: "Disconnected." });
        setOpen(false);
      } else {
        setStatus({ tone: "err", text: res.error });
      }
    });
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className={`rounded-full border px-2.5 py-1 text-xs font-medium transition ${
            configured
              ? "border-edge bg-ink/40 text-muted hover:bg-ink/60"
              : "border-accent/50 bg-accent/15 text-accent hover:bg-accent/25"
          }`}
        >
          {configured ? "Edit" : "Connect"}
        </button>
        {configured && (
          <button
            type="button"
            onClick={disconnect}
            disabled={pending}
            className="rounded-full border border-edge bg-ink/40 px-2.5 py-1 text-xs font-medium text-muted transition hover:bg-ink/60"
          >
            Disconnect
          </button>
        )}
      </div>

      {status && (
        <span className={`max-w-xs text-right text-[11px] ${status.tone === "ok" ? "text-good" : "text-warn"}`}>
          {status.text}
        </span>
      )}

      {open && (
        <div className="mt-2 w-80 rounded-lg border border-edge bg-ink/60 p-3 text-left">
          <CredentialSetup
            connector={connector}
            organizationId={organizationId}
            awsAccountId={awsAccountId}
          />
          <div className="space-y-3">
            {fields.map((f) => (
              <div key={f.name}>
                <label className="block text-[11px] font-medium text-muted" htmlFor={`${connector}-${f.name}`}>
                  {f.label}
                  {f.required && <span className="text-warn"> *</span>}
                </label>
                {f.type === "checkbox" ? (
                  <input
                    id={`${connector}-${f.name}`}
                    type="checkbox"
                    checked={values[f.name] === "true"}
                    onChange={(e) =>
                      setValues((v) => ({ ...v, [f.name]: e.target.checked ? "true" : "false" }))
                    }
                    className="mt-1"
                  />
                ) : (
                  <input
                    id={`${connector}-${f.name}`}
                    type="text"
                    value={values[f.name] ?? ""}
                    placeholder={f.placeholder}
                    onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                    className="mt-1 w-full rounded border border-edge bg-bg px-2 py-1 text-xs outline-none focus:border-accent/60"
                  />
                )}
                {f.hint && <p className="mt-1 text-[10px] leading-snug text-muted">{f.hint}</p>}
              </div>
            ))}
          </div>
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded border border-edge px-2 py-1 text-xs text-muted hover:bg-ink/60"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={submit}
              disabled={pending}
              className="rounded border border-accent/50 bg-accent/15 px-2 py-1 text-xs font-medium text-accent hover:bg-accent/25 disabled:opacity-50"
            >
              {pending ? "Saving…" : "Save"}
            </button>
          </div>
          <p className="mt-2 text-[10px] leading-snug text-muted">
            Credentials are stored by reference. Paste a secret manager pointer, never the key
            itself.
          </p>
        </div>
      )}
    </div>
  );
}
