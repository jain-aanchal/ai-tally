// SPDX-License-Identifier: Apache-2.0
"use server";

// Server actions for the connectors page (CTO-107). Toggling a cost-layer connector here flips a
// row in the gateway's tenant_connectors table; the dashboard's "Partial data" banner reads the
// same table, so the next page render reflects the change.
import { revalidatePath } from "next/cache";

import { LAYERS, type Layer } from "@/lib/cost";
import { setConnectorEnabled } from "@/lib/tenant";

export interface ToggleResult {
  ok: boolean;
  error?: string;
}

/** Server action invoked from the client toggle. Revalidates so banners refresh on next load. */
export async function toggleConnectorAction(
  layer: string,
  enabled: boolean,
): Promise<ToggleResult> {
  if (!(LAYERS as readonly string[]).includes(layer)) {
    return { ok: false, error: `unknown layer: ${layer}` };
  }
  const result = await setConnectorEnabled(layer as Layer, enabled);
  if (!result.ok) return { ok: false, error: result.error };
  // Both surfaces (Home, Cost) consult the banner state — revalidate everything served by the
  // dashboard so a toggle is visible without a hard refresh.
  revalidatePath("/", "layout");
  return { ok: true };
}
