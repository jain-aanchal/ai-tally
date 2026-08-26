// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";

import { Card } from "@/components/Card";
import { apiGet } from "@/lib/api";
import type { GuardrailRule } from "@/lib/guardrails";

import { GuardrailConfig } from "./GuardrailConfig";

interface GuardrailsPayload {
  rules: GuardrailRule[];
  configRefreshSeconds: number;
}

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const { rules } = await apiGet<GuardrailsPayload>("/api/guardrails");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Settings — Guardrails</h1>
        <p className="mt-1 text-sm text-muted">
          Cost and step caps per agent or feature. Start in observe-only, watch what would have
          fired, then graduate to enforcement with confidence.
        </p>
      </div>

      <Card title="Guardrail rules">
        <GuardrailConfig initialRules={rules} />
      </Card>

      {/* Budgets are their own route (CTO-208, F4) rather than another panel here: they are read
          by the spend surfaces, so they need a URL worth linking to from elsewhere. */}
      <p className="text-xs text-muted">
        Looking for spending budgets?{" "}
        <Link href="/settings/budgets" className="text-accent hover:underline">
          Settings — Budgets
        </Link>
        .
      </p>
    </div>
  );
}
