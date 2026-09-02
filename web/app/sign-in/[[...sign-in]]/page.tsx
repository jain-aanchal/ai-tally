// SPDX-License-Identifier: Apache-2.0
// Clerk-hosted sign-in (Initiative 1, §7). Public route; no ai-tally tenant exists until the user is
// in an organization.
import { SignIn } from "@clerk/nextjs";

export default function SignInPage() {
  return (
    <div className="flex min-h-[70vh] items-center justify-center">
      <SignIn />
    </div>
  );
}
