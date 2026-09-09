// SPDX-License-Identifier: Apache-2.0
// Is this session's workspace provisioned yet (#358)?
//
// The recovery screen the root layout renders during the provisioning race polls this. It exists
// so the browser can ask the question repeatedly without re-rendering a whole dashboard page, and
// so the answer distinguishes the two cases a customer currently cannot tell apart:
//
//   pending  the org is real, the tenant row is not there YET. Clerk's organization.created webhook
//            is asynchronous and the post-signup redirect is not, so this is the expected state for
//            a few seconds after signup and it self-heals.
//   failed   resolution broke in some other way (the gateway is down, or provisioning itself is
//            failing, e.g. the HMAC key provider answering 503). This does NOT self-heal, and
//            telling a customer to keep waiting for it would be a lie.
//
// The reason string is derived from the status code, never from an exception message or a stack:
// this response reaches a browser. The shapes and the wording live in lib/provisioning.ts, because
// a route file may export only route fields.
import { NextResponse } from "next/server";

import { NoActiveOrgError, TenantNotProvisionedError, getTenant } from "@/lib/getTenant";
import {
  PENDING_REASON,
  type ProvisioningStatusPayload,
  provisioningFailureReason,
} from "@/lib/provisioning";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(): Promise<NextResponse<ProvisioningStatusPayload>> {
  try {
    await getTenant();
    return NextResponse.json({ state: "ready" as const, reason: null });
  } catch (err) {
    if (err instanceof TenantNotProvisionedError) {
      return NextResponse.json({ state: "pending" as const, reason: PENDING_REASON });
    }
    if (err instanceof NoActiveOrgError) {
      // The docstring's own instruction: callers send the user to select-or-create-org.
      return NextResponse.json({ state: "no_org" as const, reason: null });
    }
    return NextResponse.json({
      state: "failed" as const,
      reason: provisioningFailureReason(err),
    });
  }
}
