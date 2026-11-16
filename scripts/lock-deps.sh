#!/usr/bin/env bash
# Regenerate dependency lockfiles from pyproject.toml.
#
# Run this after changing any dependency in pyproject.toml.
# Commit the updated lockfiles alongside your pyproject.toml changes.
#
# Requires: uv at exactly UV_VERSION. The lockfiles depend on how uv resolves
# --python-version and markers and prints its header, so CI's lockfile gate
# installs this same version, read from this line (#2777). Bump it deliberately.
UV_VERSION=0.10.11

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
    echo "ERROR: uv ${UV_VERSION} is required. Install it outside the project's venvs, e.g. pipx install --force uv==${UV_VERSION}"
    exit 1
fi
FOUND_UV="$(uv --version | awk '{print $2}')"
if [ "$FOUND_UV" != "$UV_VERSION" ]; then
    echo "ERROR: uv ${FOUND_UV} found; the lockfiles are made with uv ${UV_VERSION}. Install it outside the project's venvs, e.g. pipx install --force uv==${UV_VERSION}"
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

# The image's lockfile: the cloud extra alone, so no package only the test
# extra needs ships to production (pytest does, as a runtime dependency of
# opik). cloud.txt above keeps the test extra for the CI jobs (#2231), and
# constrains this compile, so every pin the image shares with it is the one
# the cloud test jobs ran.
echo "  requirements/image.txt (cloud, constrained by cloud.txt)"
uv pip compile pyproject.toml \
    --extra cloud \
    -c requirements/cloud.txt \
    --python-version "$PYTHON_VERSION" \
    --python-platform linux \
    -o requirements/image.txt \
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
