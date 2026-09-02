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

  const res = await fetch(`${GATEWAY_URL}/v1/tenant/provision`, {
    method: "POST",
    headers: { "content-type": "application/json", ...serviceTokenHeader() },
    body: JSON.stringify({ clerk_org_id: org.id, name: org.name ?? org.id }),
  });

  if (!res.ok) {
    // Return a 5xx so Clerk's own retry/backoff applies (the provision is idempotent, so a retry is
    // safe). Never fabricate a success.
    return new Response("provision failed", { status: 502 });
  }
  return new Response(null, { status: 200 });
}
