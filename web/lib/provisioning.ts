// SPDX-License-Identifier: Apache-2.0
// The wire shape and the customer-facing wording for "is this session's workspace provisioned yet"
// (#358). Pure, and separate from the route handler because a Next.js route file may export only
// route fields, and separate from the client screen because the screen must not import a server
// module. Both sides import it here.

export type ProvisioningState = "ready" | "pending" | "no_org" | "failed";

export interface ProvisioningStatusPayload {
  state: ProvisioningState;
  /** Why, for the states that need explaining. Null when the state speaks for itself. */
  reason: string | null;
}

/** The `pending` explanation. The 404 is expected for a few seconds after signup, and only then. */
export const PENDING_REASON =
  "this organization has no workspace yet, which is expected for a few seconds after signup";

/**
 * Customer-facing reason for a resolution failure that is NOT the race.
 *
 * `resolveOrgToTenant` puts the status code in its message, which is the one detail worth keeping,
 * so it is matched out deliberately and everything else about the error is dropped. An unmatched
 * error yields the generic sentence rather than its own message, because an arbitrary exception
 * message is exactly the sort of thing that turns into a leaked internal detail on a screen.
 */
export function provisioningFailureReason(err: unknown): string {
  const message = err instanceof Error ? err.message : "";
  const status = /HTTP (\d{3})/.exec(message)?.[1];
  if (status) {
    return `the control plane answered HTTP ${status} when we asked which workspace this organization belongs to`;
  }
  return "the control plane could not be reached to look up this organization's workspace";
}
