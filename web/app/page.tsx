// SPDX-License-Identifier: Apache-2.0
import { apiGet } from "@/lib/api";
import { queryEnabledConnectors } from "@/lib/tenant";
import { HomeLive, type HomePayload } from "./Live";

export default async function HomePage() {
  const endpoint = "/api/home";
  const [initialData, enabledLayers] = await Promise.all([
    apiGet<HomePayload>(endpoint),
    queryEnabledConnectors(),
  ]);
  return (
    <HomeLive endpoint={endpoint} initialData={initialData} enabledLayers={enabledLayers} />
  );
}
