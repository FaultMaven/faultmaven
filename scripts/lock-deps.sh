#!/usr/bin/env bash
# Regenerate dependency lockfiles from pyproject.toml.
#
# Run this after changing any dependency in pyproject.toml.
# Commit the updated lockfiles alongside your pyproject.toml changes.
#
# Requires: uv (pip install uv)

set -euo pipefail

PYTHON_VERSION="3.11"
cd "$(dirname "$0")/.."

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

echo "  requirements/enterprise.txt (enterprise + test)"
uv pip compile pyproject.toml \
    --extra enterprise \
    --extra test \
    --python-version "$PYTHON_VERSION" \
    --python-platform linux \
    -o requirements/enterprise.txt \
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
