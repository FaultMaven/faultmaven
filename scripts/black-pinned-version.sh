#!/bin/bash
#
# Print the black version pinned in pyproject.toml — the single source of truth
# shared by .githooks/pre-commit and scripts/install-git-hooks.sh (both warn on
# drift from this, since CI runs `black --check`). Prints nothing if unknown;
# callers treat empty as "skip the drift check".
set -uo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
grep -oE 'black==[0-9][0-9A-Za-z.]+' "$ROOT/pyproject.toml" 2>/dev/null | head -1 | sed 's/black==//'
