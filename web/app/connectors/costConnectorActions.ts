// SPDX-License-Identifier: Apache-2.0
"use server";

// Server actions for the cloud cost-connector forms (CTO-176). These persist per-tenant connector
// config through the gateway; nothing is written from the browser and no raw credential is ever
// accepted — every credential field is a secret-manager reference.
import { revalidatePath } from "next/cache";

import {
  type ConnectResult,
  connectCostConnector,
  disconnectCostConnector,
  isConfigurable,
} from "@/lib/costConnectors";

export async function connectCostConnectorAction(
  connector: string,
  fields: Record<string, string>,
): Promise<ConnectResult> {
  if (!isConfigurable(connector)) {
    return { ok: false, error: `unknown connector: ${connector}` };
  }
  // Drop empty strings so the gateway sees "absent" rather than "" and its required-field checks
  // fire with the right message.
  const payload: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(fields)) {
    if (v === "") continue;
    if (k === "emit_egress" || k === "enabled") {
      payload[k] = v === "true";
      continue;
    }
    if (k === "tag_filter" || k === "label_filter") {
      try {
        payload[k] = JSON.parse(v);
      } catch {
        return { ok: false, error: `${k} must be valid JSON, for example {"tally:workload":"ai"}` };
      }
      continue;
    }
    payload[k] = v;
  }
  const result = await connectCostConnector(connector, payload);
  if (!result.ok) return result;
  revalidatePath("/connectors");
  // The cost layers this feeds show up on Home and /cost too.
  revalidatePath("/", "layout");
  return result;
}

export async function disconnectCostConnectorAction(connector: string): Promise<ConnectResult> {
  if (!isConfigurable(connector)) {
    return { ok: false, error: `unknown connector: ${connector}` };
  }
  const result = await disconnectCostConnector(connector);
  if (!result.ok) return result;
  revalidatePath("/connectors");
  revalidatePath("/", "layout");
  return result;
}
