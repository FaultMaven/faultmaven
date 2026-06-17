#!/bin/bash
#
# Install FaultMaven's version-controlled git hooks.
#
# Points git at the tracked .githooks/ directory (core.hooksPath) so the
# pre-commit black formatter stays in sync with the repo — no per-clone copy
# into .git/hooks/ that can drift. Run once per clone:
#
#     ./scripts/install-git-hooks.sh
#
# To switch to the pre-commit framework instead (.pre-commit-config.yaml):
#     git config --unset core.hooksPath && pip install pre-commit && pre-commit install

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if [ ! -d .githooks ]; then
    echo "✗ .githooks/ not found at repo root ($ROOT)" >&2
    exit 1
fi

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks
echo "✓ git hooks installed (core.hooksPath → .githooks)"

# Verify black matches the version CI enforces (formatting is version-sensitive).
PINNED="$(grep -oE 'black==[0-9][0-9A-Za-z.]+' pyproject.toml | head -1 | sed 's/black==//')"
if [ -x .venv/bin/black ] && .venv/bin/black --version >/dev/null 2>&1; then
    HAVE="$(.venv/bin/black --version | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)"
    if [ "$HAVE" = "$PINNED" ]; then
        echo "✓ .venv black $HAVE matches pinned $PINNED (CI runs 'black --check')"
    else
        echo "⚠ .venv black $HAVE != pinned $PINNED — run: .venv/bin/pip install black==$PINNED"
    fi
else
    echo "⚠ black not found in .venv — run: pip install -e \".[dev]\"  (installs black==$PINNED)"
fi
