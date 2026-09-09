// SPDX-License-Identifier: Apache-2.0
// Clerk provisioning webhook (Initiative 1, §4). Clerk emits `organization.created` here, svix-
// signed. The gateway is private in the hosted topology (only web is public), so Clerk cannot reach
// it directly: this thin public route verifies the svix signature, then forwards the VERIFIED event
// to the gateway's service-token-authed POST /v1/tenant/provision.
//
// Never trust the body before the signature verifies. This route carries no Clerk session (it is
// listed public in middleware) and is authenticated solely by its svix signature.

import { Webhook } from "svix";

import { serviceTokenHeader } from "@/lib/getTenant";

const GATEWAY_URL = process.env.TALLY_GATEWAY_URL ?? "http://localhost:8080";

/** How long to wait on the gateway's provision call before giving Clerk a retriable 502. */
const PROVISION_TIMEOUT_MS = 10_000;

interface ClerkOrgEvent {
  type: string;
  data: { id?: string; name?: string };
}

export async function POST(req: Request): Promise<Response> {
  const secret = process.env.CLERK_WEBHOOK_SIGNING_SECRET;
  if (!secret) {
    // Misconfiguration, not a client error. A 500 makes Clerk retry once the secret is set.
    return new Response("webhook signing secret not configured", { status: 500 });
  }

  const svixId = req.headers.get("svix-id");
  const svixTimestamp = req.headers.get("svix-timestamp");
  const svixSignature = req.headers.get("svix-signature");
  if (!svixId || !svixTimestamp || !svixSignature) {
    return new Response("missing svix headers", { status: 400 });
  }

  const payload = await req.text();
  let event: ClerkOrgEvent;
  try {
    event = new Webhook(secret).verify(payload, {
      "svix-id": svixId,
      "svix-timestamp": svixTimestamp,
      "svix-signature": svixSignature,
    }) as ClerkOrgEvent;
  } catch {
    // Signature did not verify. Reject; do not touch the body.
    return new Response("invalid signature", { status: 401 });
  }

  // Only provision on org creation. Ack every other event so Clerk does not redeliver it.
  if (event.type !== "organization.created") {
    return new Response(null, { status: 200 });
  }

  const org = event.data;
  if (!org?.id) {
    return new Response("event missing organization id", { status: 400 });
  }

  // CTO-359. Bound the call and own its failure. Reaching the gateway is the step most likely to
  // fail in the hosted topology (it is private, and a deploy cycles its tasks), and an unhandled
  // rejection here surfaced as a bare 500 with a stack in the logs, indistinguishable from a bug in
  // this route. Verified against the local stack: with nothing listening on the gateway port this
  // route returned 500 from a `TypeError: fetch failed` rather than the deliberate 502 below.
  //
  // The timeout matters separately. Provision is synchronous all the way to the key provider, which
  // under TALLY_HMAC_KEY_PROVIDER=kms is two Secrets Manager calls at botocore's tens-of-seconds
  // defaults. Without a bound, a slow gateway holds this invocation until the platform kills it,
  // which on Vercel bills the full duration and gives Clerk a 504 instead of an answer. 10s is well
  // clear of a healthy provision (single-digit milliseconds locally) and well under Clerk's own
  // delivery timeout, so a stall becomes a prompt, retriable 502.
  let res: Response;
  try {
    res = await fetch(`${GATEWAY_URL}/v1/tenant/provision`, {
      method: "POST",
      headers: { "content-type": "application/json", ...serviceTokenHeader() },
      body: JSON.stringify({ clerk_org_id: org.id, name: org.name ?? org.id }),
      signal: AbortSignal.timeout(PROVISION_TIMEOUT_MS),
    });
  } catch {
    // Unreachable, or it did not answer in time. Same contract as a gateway error: a 5xx so Clerk
    // retries, and provisioning is idempotent so the retry is safe. Never fabricate a success.
    return new Response("provision unreachable", { status: 502 });
  }

  if (!res.ok) {
    // Return a 5xx so Clerk's own retry/backoff applies (the provision is idempotent, so a retry is
    // safe). Never fabricate a success.
    return new Response("provision failed", { status: 502 });
  }
  return new Response(null, { status: 200 });
}
