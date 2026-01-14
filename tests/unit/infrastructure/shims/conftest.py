"""Minimal conftest for shim tests.

These tests are designed to run with minimal dependencies
to verify shim graceful degradation behavior.
"""

import pytest

# Override the parent conftest by being in a child directory
# This allows shim tests to run in isolation
