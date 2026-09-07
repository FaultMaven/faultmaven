"""Shared doubles for the auth module's unit tests.

Lifted here from ``services/test_team_invitations.py`` (fm#1365 review, C9):
three copies of an enterprise stub across three modules is three chances for
one of them to disagree with the schema, and a hand-rolled user store is what
hid the ``casefold``/``lower`` mismatch of A7 — its private copy of the lookup
matched what the *service* did rather than what the repository index does.

The rule these follow: a double may be simpler than the real thing, but it must
not be more permissive. Where the production implementation refuses (a member
anchored to another enterprise, a second live invitation for one address), the
double refuses too, and says so loudly rather than silently accepting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import pytest

from faultmaven.models.interfaces_user import Enterprise
from tests.utils import InMemoryRevocationStore


@pytest.fixture
def in_memory_revocation_store() -> InMemoryRevocationStore:
    """A revocation store implementing the full contract, backed by dicts."""
    return InMemoryRevocationStore()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FakeEnterpriseRepository:
    """The one field the invitation rule reads off an enterprise: ``domain``.

    Built from real :class:`Enterprise` models rather than from ``Mock``s, so a
    field that changes shape in the schema fails here instead of being answered
    by a mock that accepts anything.
    """

    def __init__(self, enterprises: Dict[str, Optional[str]]):
        """``{enterprise_id: domain or None}`` — ``None`` is a personal tenant."""
        now = utcnow()
        self._rows = {
            enterprise_id: Enterprise(
                enterprise_id=enterprise_id,
                name=enterprise_id,
                slug=enterprise_id,
                domain=domain,
                created_at=now,
                updated_at=now,
            )
            for enterprise_id, domain in enterprises.items()
        }

    async def get_enterprise(self, enterprise_id: str) -> Optional[Enterprise]:
        return self._rows.get(enterprise_id)


@pytest.fixture
def enterprise_repository_factory():
    """Build a :class:`FakeEnterpriseRepository` from ``{id: domain}``."""
    return FakeEnterpriseRepository
