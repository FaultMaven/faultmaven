#!/usr/bin/env bash
# Regenerate dependency lockfiles from pyproject.toml.
#
# Run this after changing any dependency in pyproject.toml.
# Commit the updated lockfiles alongside your pyproject.toml changes.
#
# Requires: uv (pip install uv)

set -euo pipefail

cd "$(dirname "$0")/.."

# Lock for pyproject.toml's requires-python floor, read from it so there is one
# copy: uv reads a bare "3.11" as 3.11.0, below the floor (#2773).
PYTHON_VERSION="$(sed -n 's/^requires-python = ">=\([0-9][0-9.]*\)"$/\1/p' pyproject.toml)"
if [ -z "$PYTHON_VERSION" ]; then
    echo "ERROR: no requires-python = \">=X.Y.Z\" line in pyproject.toml to lock for"
    exit 1
fi

if ! command -v uv &> /dev/null; then
    echo "ERROR: uv is required. Install with: pip install uv"
    exit 1
fi

echo "Generating lockfiles for Python ${PYTHON_VERSION}..."

echo "  requirements/test.txt (community + test)"
uv pip compile pyproject.toml \
    --extra test \
    --python-version "$PYTHON_VERSION" \
    --python-platform linux \
    -o requirements/test.txt \
    --quiet

echo "  requirements/cloud.txt (cloud + test)"
uv pip compile pyproject.toml \
    --extra cloud \
    --extra test \
    --python-version "$PYTHON_VERSION" \
    --python-platform linux \
    -o requirements/cloud.txt \
    --quiet

echo "  requirements/dev.txt (dev + test)"
uv pip compile pyproject.toml \
    --extra dev \
    --extra test \
    --python-version "$PYTHON_VERSION" \
    --python-platform linux \
    -o requirements/dev.txt \
    --quiet

echo ""
echo "Done. Lockfiles updated in requirements/"
echo "Commit these alongside your pyproject.toml changes."
