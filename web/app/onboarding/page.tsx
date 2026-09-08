// SPDX-License-Identifier: Apache-2.0
import { CoveragePanel } from "@/components/CoveragePanel";
import { apiGet } from "@/lib/api";
import type { FunnelEvent, OnboardingProgress, TenantProxyCredentials } from "@/lib/onboarding";

import { Onboarding } from "./Onboarding";

interface OnboardingPayload {
  progress: OnboardingProgress;
  creds: TenantProxyCredentials;
  funnel: FunnelEvent[];
}

export const dynamic = "force-dynamic";

export default async function OnboardingPage() {
  const { progress, creds } = await apiGet<OnboardingPayload>("/api/onboarding");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Get started</h1>
        <p className="mt-1 text-sm text-muted">
          Two steps to your first dashboard. Most teams see their first trace in under five minutes.
        </p>
      </div>
      <Onboarding initialProgress={progress} creds={creds} />
      {/* Per-layer coverage (CTO-261, §4.1). Sits under the connect steps because it answers the
          question that comes NEXT: the steps above get the LLM layer flowing, and this says which
          of the remaining layers a span actually proves. It polls on the client, so the page does
          not block on the probe. */}
      <CoveragePanel />
    </div>
  );
}
