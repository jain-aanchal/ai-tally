#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Restore examples/aider-demo/target-repo to its pristine fixture state between
# demo tasks. The fixture lives in the parent ai-tally git repo (target-repo is
# NOT a nested git repo), so we just checkout HEAD for that subtree and clean.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
target_rel="examples/aider-demo/target-repo"

cd "$repo_root"
git checkout HEAD -- "$target_rel"
git clean -fd "$target_rel" >/dev/null
echo "  reset: $target_rel restored"
