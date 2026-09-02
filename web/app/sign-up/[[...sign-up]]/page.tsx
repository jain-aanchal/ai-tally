// SPDX-License-Identifier: Apache-2.0
// Clerk-hosted sign-up (Initiative 1, §7). Public route.
import { SignUp } from "@clerk/nextjs";

export default function SignUpPage() {
  return (
    <div className="flex min-h-[70vh] items-center justify-center">
      <SignUp />
    </div>
  );
}
