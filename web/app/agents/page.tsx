// SPDX-License-Identifier: Apache-2.0
// The standalone Agents workflow was folded into the unified Cost explorer as the `agent` group-by
// dimension (CTO-241, M2 of CTO-239). This route now redirects to /cost?groupBy=agent so old links,
// bookmarks, and the retired nav item all land on the explorer's cost-by-agent view. The run
// drill-down (/agents/runs/[runId]) and its API are deliberately kept and untouched.

import { redirect } from "next/navigation";

export default function AgentsPage() {
  redirect("/cost?groupBy=agent");
}
