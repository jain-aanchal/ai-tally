// SPDX-License-Identifier: Apache-2.0
// Shared diagnostics-row helper, hoisted from three verbatim-ish copies in /cost, /compare and
// /estimate (CTO-243). Renders a labelled key/value pair for the small "diagnostics" dl blocks.
//
// Two call shapes exist and both must stay pixel-identical:
//   - default: emits a bare <dt>/<dd> fragment, for a two-column grid <dl> (compare, estimate) where
//     dt and dd are direct grid children.
//   - row: wraps the pair in a flex row, for a `space-y` <dl> (cost/FeatureDetail) where each entry
//     is one spaced-out line.

import type { ReactNode } from "react";

// `v` is a node rather than a string so a caller can pass the shared `Blank` primitive for a value
// it does not have (CTO-298). A diagnostics row whose number is unknown has to be able to say so
// with its reason attached, the same way every other honest blank in the app does.
export function Diag({
  k,
  v,
  good,
  row,
}: {
  k: string;
  v: ReactNode;
  good?: boolean;
  row?: boolean;
}) {
  const inner = (
    <>
      <dt className="text-muted">{k}</dt>
      <dd className={good ? "text-good" : ""}>{v}</dd>
    </>
  );
  return row ? <div className="flex items-baseline justify-between">{inner}</div> : inner;
}
