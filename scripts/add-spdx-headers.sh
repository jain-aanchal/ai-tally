#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Add SPDX-License-Identifier headers to first-party source files.
#
# Idempotent: skips files that already declare any SPDX-License-Identifier line.
# Respects shebangs (header goes after the shebang line).
#
# Scope:
#   - Python:  sdk/python/src/tally/**.py, sdk/python/tests/**.py
#   - Go:      infra/edge-proxy/**.go (excluding vendor/)
#   - TS/JS:   web/**.{ts,tsx,js,jsx,mjs,cjs} (excluding node_modules/, .next/)
#
# Skipped categories: package.json, tsconfig.json, generated files, vendored
# directories, config JSON.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY_HEADER='# SPDX-License-Identifier: Apache-2.0'
SLASH_HEADER='// SPDX-License-Identifier: Apache-2.0'

added=0
skipped=0

add_hash_header() {
    local file="$1" header="$2"
    if grep -q 'SPDX-License-Identifier' "$file"; then
        skipped=$((skipped + 1)); return
    fi
    local first
    first="$(head -n1 "$file")"
    if [[ "$first" == "#!"* ]]; then
        # Insert after shebang.
        { head -n1 "$file"; echo "$header"; tail -n +2 "$file"; } > "$file.tmp"
    else
        { echo "$header"; cat "$file"; } > "$file.tmp"
    fi
    mv "$file.tmp" "$file"
    added=$((added + 1))
}

add_slash_header() {
    local file="$1" header="$2"
    if grep -q 'SPDX-License-Identifier' "$file"; then
        skipped=$((skipped + 1)); return
    fi
    { echo "$header"; cat "$file"; } > "$file.tmp"
    mv "$file.tmp" "$file"
    added=$((added + 1))
}

# Python.
while IFS= read -r f; do
    add_hash_header "$f" "$PY_HEADER"
done < <(find sdk/python/src/tally sdk/python/tests -type f -name '*.py' 2>/dev/null)

# Go.
while IFS= read -r f; do
    add_slash_header "$f" "$SLASH_HEADER"
done < <(find infra/edge-proxy -type f -name '*.go' -not -path '*/vendor/*' 2>/dev/null)

# TS/JS in web.
while IFS= read -r f; do
    add_slash_header "$f" "$SLASH_HEADER"
done < <(find web -type f \( -name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.jsx' -o -name '*.mjs' -o -name '*.cjs' \) \
    -not -path '*/node_modules/*' \
    -not -path '*/.next/*' \
    -not -name 'next-env.d.ts' \
    2>/dev/null)

echo "add-spdx-headers: $added added, $skipped skipped."
