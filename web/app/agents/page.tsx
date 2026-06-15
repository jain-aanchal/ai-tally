// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { AgentsLive, type AgentsPayload } from "./Live";

export default async function AgentsPage({
  searchParams,
}: {
  searchParams?: Promise<{ tag?: string; run?: string }>;
}) {
  // Forward ?tag= / ?run= to the API so the table is pre-filtered (CTO-104 deep links).
  const sp = (await searchParams) ?? {};
  const qs = new URLSearchParams();
  if (sp.tag) qs.set("tag", sp.tag);
  if (sp.run) qs.set("run", sp.run);
  const query = qs.toString() ? `?${qs.toString()}` : "";
  const endpoint = `/api/agents${query}`;
  const initialData = await apiGet<AgentsPayload>(endpoint);
  return <AgentsLive endpoint={endpoint} initialData={initialData} />;
}
