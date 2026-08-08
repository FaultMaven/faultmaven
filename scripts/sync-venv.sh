#!/usr/bin/env bash
#
# Bring a virtualenv to exactly what a lockfile pins, and stamp it.
#
# The consumer-side counterpart to lock-deps.sh: that one regenerates
# requirements/*.txt from pyproject.toml, this one makes an environment match
# one of them.
#
#     ./scripts/sync-venv.sh dev            # .venv-dev   from requirements/dev.txt
#     ./scripts/sync-venv.sh cloud          # .venv-cloud from requirements/cloud.txt
#     ./scripts/sync-venv.sh test .venv     # explicit target
#
# Why a venv PER lockfile rather than one for everything: no lockfile is a
# superset. mypy and import-linter are in dev only; boto3, opik and presidio are
# in cloud only. CI has the same split — Code Quality runs on dev.txt, Test
# Cloud on cloud.txt — so one local environment cannot mirror every job.
#
# Uses `uv pip sync`, not `pip install -r`. Both fix versions (pip does downgrade
# to an exact pin), but only sync REMOVES packages the lockfile omits. Leftovers
# matter: a package you have and CI does not can make an optional import succeed
# locally and fail there.
#
# Requires: uv (pip install uv)

set -euo pipefail

PYTHON_VERSION="3.11"
cd "$(dirname "$0")/.."
ROOT="$PWD"

EXTRA="${1:-}"
if [ -z "$EXTRA" ]; then
    echo "usage: ./scripts/sync-venv.sh <dev|test|cloud> [venv-path]" >&2
    echo "" >&2
    echo "  dev    requirements/dev.txt    — mirrors Code Quality, Architecture Boundary, Security Scanning" >&2
    echo "  test   requirements/test.txt   — mirrors Test Standalone, Test Packaging Configuration" >&2
    echo "  cloud  requirements/cloud.txt  — mirrors Test Cloud, Test PostgreSQL Integration" >&2
    exit 1
fi

LOCKFILE="requirements/${EXTRA}.txt"
VENV="${2:-.venv-${EXTRA}}"

if [ ! -f "$LOCKFILE" ]; then
    echo "✗ no such lockfile: $LOCKFILE" >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "✗ uv is required. Install with: pip install uv" >&2
    exit 1
fi

# Refuse to overwrite a venv that is not one of ours unless it was created here.
# `.venv` is the shared one the pre-commit hook looks for and several worktrees
# may be using it; clobbering it mid-run is exactly the surprise this tooling is
# supposed to prevent, so it takes an explicit second argument to reach.
if [ -e "$VENV" ] && [ ! -x "$VENV/bin/python" ]; then
    echo "✗ $VENV exists but is not a virtualenv — refusing to overwrite" >&2
    exit 1
fi

echo "Syncing $VENV to $LOCKFILE (Python ${PYTHON_VERSION})..."

if [ ! -x "$VENV/bin/python" ]; then
    uv venv "$VENV" --python "$PYTHON_VERSION" --quiet
fi

# Two steps, mirroring what CI does: the lockfile decides every version, then the
# project goes in with --no-deps so the ranges in pyproject.toml never resolve at
# install time. That is what makes the lockfile the single source of versions.
uv pip sync --python "$VENV/bin/python" "$LOCKFILE" --quiet
uv pip install --python "$VENV/bin/python" -e . --no-deps --quiet

# Stamp what this environment was built from. Written in `sha256sum` format with
# a repo-relative path so it can be checked directly:
#     sha256sum -c --status .venv-dev/.locksum
# The post-merge and post-checkout hooks read it to warn when the lockfile moves
# and the environment does not.
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$LOCKFILE" > "$VENV/.locksum"
elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$LOCKFILE" > "$VENV/.locksum"
else
    echo "⚠ neither sha256sum nor shasum found — no stamp written," >&2
    echo "  so the staleness hooks cannot warn about $VENV" >&2
fi

COUNT="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(len(list(m.distributions())))')"
echo "✓ $VENV now matches $LOCKFILE ($COUNT packages)"
echo "  Use it with: $VENV/bin/python -m pytest ..."
