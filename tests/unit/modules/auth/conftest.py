"""Shared fixtures for the auth-module unit tests."""

from __future__ import annotations

import pytest

from tests.utils import InMemoryRevocationStore


@pytest.fixture
def in_memory_revocation_store() -> InMemoryRevocationStore:
    """A revocation store implementing the full contract, backed by dicts."""
    return InMemoryRevocationStore()
