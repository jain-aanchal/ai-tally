// SPDX-License-Identifier: Apache-2.0
// Typed API fetcher for server components.
//
// Pages fetch over HTTP so swapping to a real backend later is just a base-URL change. In server
// components we derive the base URL from the incoming request (`headers()`); in dev/build with no
// request we fall back to NEXT_PUBLIC_API_BASE_URL or localhost.
//
// THE SESSION HAS TO RIDE ALONG (CTO-269). This is a server-to-self fetch: the page is already
// running on the server, and it calls its own route handler over HTTP. That second request is a
// brand new one, so it carries nothing from the browser unless we put it there. `clerkMiddleware`
// runs on `/api/*` and answers an unauthenticated non-document request with 404 rather than a
// redirect, so without the cookie every server-rendered page fails with
// `API /api/home failed: 404` no matter who is signed in.
//
// It went unnoticed because the only configuration anyone ran locally set TALLY_DEV_TENANT, which
// makes the middleware a no-op, so the credential-less self-fetch sailed through. The first
// deployment with real auth would have hit it on all thirteen pages at once.
//
// Only the cookie is forwarded, deliberately. It is what carries the Clerk session, the request is
// same-origin back into this app, and forwarding the whole header set would drag along host,
// content-length and encoding headers that describe the ORIGINAL request and misdescribe this one.

import { headers } from "next/headers";

/** The incoming request's headers, or null when there is no request (build time, some tests). */
async function incoming(): Promise<Headers | null> {
  try {
    return await headers();
  } catch {
    return null;
  }
}

function baseUrlFrom(h: Headers | null): string {
  if (process.env.NEXT_PUBLIC_API_BASE_URL)
    return process.env.NEXT_PUBLIC_API_BASE_URL;
  if (h) {
    const host = h.get("host") ?? "localhost:3217";
    const proto = h.get("x-forwarded-proto") ?? "http";
    return `${proto}://${host}`;
  }
  return process.env.PORT
    ? `http://localhost:${process.env.PORT}`
    : "http://localhost:3217";
}

export async function apiGet<T>(path: string): Promise<T> {
  const h = await incoming();
  const base = baseUrlFrom(h);
  // No cookie means no request context (build time) or a genuinely anonymous caller. Send the
  // request either way and let the route decide: fabricating a session here would be worse.
  const cookie = h?.get("cookie");
  const res = await fetch(`${base}${path}`, {
    cache: "no-store",
    ...(cookie ? { headers: { cookie } } : {}),
  });
  if (!res.ok) throw new Error(`API ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}
