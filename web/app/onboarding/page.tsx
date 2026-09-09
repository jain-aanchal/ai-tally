// SPDX-License-Identifier: Apache-2.0
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
      {/* The coverage panel now renders inside Onboarding, because step 2 and the panel report the
          same fact and #320 was them reporting it from two different sources. One client-side poll
          feeds both; the page still does not block on the probe. */}
      <Onboarding initialProgress={progress} creds={creds} />
    </div>
  );
}
