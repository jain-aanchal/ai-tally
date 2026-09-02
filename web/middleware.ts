// SPDX-License-Identifier: Apache-2.0
// Clerk request middleware (Initiative 1, §7). Protects every app route, leaves the sign-in,
// sign-up, and Clerk provisioning webhook public, and pushes a signed-in user with no active org to
// the select-or-create-org screen (the product has no personal workspace).
//
// Dev escape hatch (§10): when TALLY_DEV_TENANT is set, this is a NO-OP so `make up` and CI reach
// every route with no Clerk session and no Clerk keys. The product path (flag unset) requires a
// session, exactly as intended.

import {
  clerkMiddleware,
  createRouteMatcher,
} from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

const DEV_TENANT = process.env.TALLY_DEV_TENANT?.trim();

// Public routes carry no Clerk session: the sign-in / sign-up flows, and the provisioning webhook,
// which is svix-authenticated inside its own route (§4), not by a Clerk session.
const isPublicRoute = createRouteMatcher([
  "/sign-in(.*)",
  "/sign-up(.*)",
  "/api/webhooks/clerk",
]);

// The screen where a signed-in user with no active org lands. Excluded from the org requirement so
// the redirect below cannot loop.
const isOrgSelectionRoute = createRouteMatcher(["/select-org(.*)"]);

const clerkGuard = clerkMiddleware(async (auth, req: NextRequest) => {
  if (isPublicRoute(req)) {
    return;
  }
  const { userId, orgId } = await auth.protect();
  // Signed in but in no organization: send them to create or pick one before any data page renders.
  if (userId && !orgId && !isOrgSelectionRoute(req)) {
    return NextResponse.redirect(new URL("/select-org", req.url));
  }
});

// A no-op middleware for the dev escape hatch. Typed to match clerkMiddleware's return.
function devPassThrough(): NextResponse {
  return NextResponse.next();
}

export default DEV_TENANT ? devPassThrough : clerkGuard;

export const config = {
  // Run on everything except Next internals and static files, plus API routes. Standard Clerk
  // matcher.
  matcher: [
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
