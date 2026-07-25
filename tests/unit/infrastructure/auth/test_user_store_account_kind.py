"""``account_kind`` survives a round-trip through the user store (ADR-012).

``DatabaseUserStore`` converts repository ``User`` → ``DevUser`` on read and
back on update, and ``UserRepository.update`` writes every column. A ``DevUser``
that does not carry ``account_kind`` therefore silently rewrites it to the
'individual' default on ANY update — demoting the Slack service account, which
in turn flips the derived ``cases.source`` for every case it later creates.

The D10 credential is issued against that very account, so these tests pin the
round-trip in both directions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.persistence.user_repository import User

pytestmark = pytest.mark.asyncio


def _repo_user(account_kind: str = "slack") -> User:
    now = datetime.now(timezone.utc)
    return User(
        user_id="user-123",
        username="slack-agent",
        email="slack-agent@faultmaven.example",
        display_name="slack-agent",
        created_at=now,
        updated_at=now,
        is_active=True,
        roles=["user"],
        account_kind=account_kind,
    )


def _store(existing: User | None = None) -> tuple[DatabaseUserStore, AsyncMock]:
    repo = AsyncMock()
    repo.get.return_value = existing
    repo.get_by_username.return_value = existing
    repo.get_by_email.return_value = None
    repo.update.side_effect = lambda user: user
    repo.save.side_effect = lambda user: user
    return DatabaseUserStore(repo), repo


class TestAccountKindRoundTrip:
    async def test_read_surfaces_account_kind(self):
        store, _ = _store(_repo_user("slack"))

        user = await store.get_user_by_username("slack-agent")

        assert user.account_kind == "slack"

    async def test_update_preserves_account_kind(self):
        """The regression: updating a service account must not demote it.

        Any update — a role change from scripts/auth/promote_to_admin.py, a
        display-name edit — used to write account_kind='individual' back.
        """
        store, repo = _store(_repo_user("slack"))
        user = await store.get_user_by_username("slack-agent")

        user.roles = ["user", "admin"]
        await store.update_user(user)

        written = repo.update.call_args.args[0]
        assert written.account_kind == "slack"

    async def test_create_can_set_account_kind(self):
        """Service accounts are created as such, never briefly as individuals."""
        store, repo = _store()

        created = await store.create_user(username="slack-agent", account_kind="slack")

        assert created.account_kind == "slack"
        assert repo.save.call_args.args[0].account_kind == "slack"

    async def test_create_defaults_to_individual(self):
        """Humans stay the default; only an explicit caller opts into 'slack'."""
        store, repo = _store()

        created = await store.create_user(username="alice")

        assert created.account_kind == "individual"
        assert repo.save.call_args.args[0].account_kind == "individual"
