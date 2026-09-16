// SPDX-License-Identifier: Apache-2.0
// Whether a control-plane write that could not be delivered may still be ECHOED back to the caller
// as though it had been accepted (CTO-393).
//
// This is a policy about WRITES, and it lives here rather than in getTenant.ts because that module
// answers a different question: which tenant the caller is acting for. The only thing the echo rule
// borrows from it is the dev escape hatch.
import "server-only";

import { devTenant } from "./getTenant";

/**
 * True only OFF the product path.
 *
 * The echo exists so a fresh clone with no infra still works, and the dev escape hatch is exactly
 * what marks that case: `TALLY_DEV_TENANT` is the only way the dashboard serves a tenant with no
 * authenticated Clerk organization behind it. `sampleDataAllowed()` in lib/mock.ts is a strict
 * subset of this (it requires an explicit demo build on top), so this is the condition that decides.
 *
 * On the product path an unreachable control plane is a FAILED SAVE, and reporting it as anything
 * else is the honesty invariant's headline failure: a cap or threshold the customer believes they
 * set, which was never stored, and which nothing downstream will ever enforce.
 */
export function controlPlaneEchoAllowed(): boolean {
  return devTenant() !== null;
}
