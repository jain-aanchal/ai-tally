// SPDX-License-Identifier: Apache-2.0
// Workflow 4 — business-outcome attribution.
//
// Reads ?tag=&provider=&outcome= and renders $/conversion per provider with
// Wilson 95% intervals on conversion rate. The chatbot demo's run.sh deep-links
// here with ?tag=chatbot-demo&outcome=positive_feedback.

import { apiGet } from "@/lib/api";
import type { AttributionReport } from "@/lib/attribution";
import { AttributionLive } from "./Live";

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function readStr(
  params: Record<string, string | string[] | undefined>,
  key: string,
): string | null {
  const v = params[key];
  if (Array.isArray(v)) return v[0] ?? null;
  return v ?? null;
}

export default async function AttributionPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const tag = readStr(params, "tag");
  const provider = readStr(params, "provider");
  const outcome = readStr(params, "outcome") ?? "conversion";

  const qs = new URLSearchParams();
  if (tag) qs.set("tag", tag);
  if (provider) qs.set("provider", provider);
  qs.set("outcome", outcome);

  const endpoint = `/api/attribution?${qs.toString()}`;
  const initialData = await apiGet<AttributionReport>(endpoint);

  return (
    <AttributionLive
      endpoint={endpoint}
      initialData={initialData}
      outcome={outcome}
      tag={tag}
      provider={provider}
    />
  );
}
