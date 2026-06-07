<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR 0001: Use the DCO, not a CLA, for contributor sign-off

- Status: Accepted
- Date: 2026-06-07

## Context

ai-tally is going public under the Apache License 2.0. Open-source projects
typically use one of two mechanisms to establish that contributors have the
right to license their patches:

- A **Contributor License Agreement (CLA)** — a legal contract each
  contributor (or their employer) signs once, often via a bot. Gives the
  project a broad, sometimes exclusive, copyright license. Examples: Apache
  Software Foundation, Google.
- The **Developer Certificate of Origin (DCO)** — a per-commit attestation
  added as a `Signed-off-by:` trailer. No separate contract; the certification
  is the text at <https://developercertificate.org/>. Examples: Linux kernel,
  Docker, Kubernetes (post-2017).

## Decision

ai-tally will use the **DCO**. Every commit in a pull request must include a
`Signed-off-by:` trailer, typically added with `git commit -s`.

## Consequences

- **Lower friction for contributors.** No external form, no waiting on
  countersignature, no separate corporate paperwork in the common case.
- **Lower governance overhead for us.** No CLA bot to host, no signed-CLA
  registry to maintain, no automated PR blocks beyond a DCO check.
- **Sufficient for our current stage.** We are early; we do not yet have the
  scale or relicensing ambitions that motivate large foundations to require
  CLAs.
- **We can revisit.** If we later want to (a) relicense, (b) dual-license, or
  (c) accept contributions under enterprise CLA terms, we can introduce a CLA
  for new contributions at that point. The DCO history remains valid.
- **CI enforcement.** We will eventually add a DCO check (e.g.
  [DCO GitHub App](https://github.com/apps/dco) or an equivalent workflow)
  so unsigned PRs are flagged automatically.
