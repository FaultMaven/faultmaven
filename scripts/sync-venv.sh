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
# in cloud only. CI has the same split — Architecture Boundary and API Contract
# Drift run on dev.txt, Test Cloud on cloud.txt — so one local environment cannot
# mirror every job.
#
# Uses `uv pip sync`, where CI uses `pip install -r`. Same end state on CI's
# empty runner; different on a reusable environment, which is the point — both
# fix versions (pip does downgrade to an exact pin), but only sync REMOVES
# packages the lockfile omits. Leftovers matter: a package you have and CI does
# not can make an optional import succeed locally and fail there.
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
    echo "  dev    requirements/dev.txt    — Architecture Boundary Check, API Contract Drift Check" >&2
    echo "  test   requirements/test.txt   — Test Standalone, Code Quality Checks," >&2
    echo "                                   Test Packaging Configuration, Environment Smoke Check" >&2
    echo "  cloud  requirements/cloud.txt  — Test Cloud, Test PostgreSQL Integration" >&2
    echo "" >&2
    echo "  (Security Scanning installs no lockfile — it audits the files themselves.)" >&2
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
UV_BIN="$(command -v uv)"

# `uv pip sync` removes everything the lockfile does not list, and uv is in none
# of them. If the uv being used lives inside the venv about to be synced, the
# first run uninstalls it and every later run dies on the check above — with the
# environment left half-synced. Install uv outside the target (pipx, or the
# system interpreter) instead.
case "$UV_BIN" in
    "$ROOT/$VENV"/*|"$VENV"/*)
        echo "✗ uv resolves to $UV_BIN, inside the venv being synced." >&2
        echo "  'uv pip sync' would uninstall it — uv is in no lockfile." >&2
        echo "  Install uv outside the target venv (e.g. pipx install uv)." >&2
        exit 1
        ;;
esac

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
else
    # PYTHON_VERSION only binds at creation, so an environment made earlier on a
    # different interpreter would be synced from a lockfile compiled for 3.11 and
    # then stamped and reported "matches". The lockfiles are resolved with
    # --python-version 3.11 --python-platform linux and carry no environment
    # markers, so nothing downstream would notice. Refuse rather than recreate:
    # deleting an environment someone may be running tests in is not this
    # script's call to make.
    HAVE="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown)"
    if [ "$HAVE" != "$PYTHON_VERSION" ]; then
        echo "✗ $VENV runs Python $HAVE, but $LOCKFILE is compiled for $PYTHON_VERSION." >&2
        echo "  Syncing anyway would install 3.11-resolved pins under $HAVE and stamp it as" >&2
        echo "  matching, which is the silent divergence this tooling exists to prevent." >&2
        echo "" >&2
        echo "  Recreate it yourself when nothing is using it:" >&2
        echo "      rm -rf $VENV && ./scripts/sync-venv.sh $EXTRA $VENV" >&2
        exit 1
    fi
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
