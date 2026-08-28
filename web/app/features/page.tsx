// SPDX-License-Identifier: Apache-2.0
// The standalone Features / business-attribution workflow was folded into the unified Cost explorer
// as the `feature` group-by dimension (CTO-242, M3 of CTO-239). This route now redirects to
// /cost?groupBy=feature so old links, bookmarks, and the retired nav item all land on the explorer's
// cost-by-feature view; narrowing to a single feature there surfaces that feature's unit economics,
// value-event config and attribution diagnostics (see app/cost/FeatureDetail.tsx). /api/features and
// the value-event config POST are deliberately kept and untouched, reused by that detail.

import { redirect } from "next/navigation";

export default function FeaturesPage() {
  redirect("/cost?groupBy=feature");
}
