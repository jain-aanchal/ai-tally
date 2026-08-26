// SPDX-License-Identifier: Apache-2.0
"use server";

// Server action behind the account search box (CTO-188, plan D2 / B6).
//
// A server action rather than a route handler or a client fetch, matching the connectors page: the
// gateway URL is server config, and the plaintext account id must not reach the browser's network
// tab or a URL. It goes into one POST body, produces hashes, and is dropped. Nothing is cached and
// nothing is revalidated, because a lookup changes no state.

import { lookupAccountHashes, type AccountLookup } from "@/lib/accountLabels";

export async function lookupAccountAction(accountId: string): Promise<AccountLookup> {
  return lookupAccountHashes(accountId);
}
