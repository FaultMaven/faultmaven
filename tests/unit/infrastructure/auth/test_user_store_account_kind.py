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


def _repo_user(account_kind: str = "slack", **overrides) -> User:
    now = datetime.now(timezone.utc)
    fields = dict(
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
    fields.update(overrides)
    return User(**fields)


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

        Any update — a role change from scripts/auth/promote_to_platform_admin.py, a
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


class TestUpdatePreservesTheStoredRecord:
    """``update_user`` must not write NULL over what DevUser doesn't model.

    DevUser is a partial view; ``UserRepository.update`` writes every column.
    Rebuilding a User from a DevUser wiped the password hash, the SSO linkage
    and the verification/login timestamps — so a role change through
    scripts/auth/promote_to_platform_admin.py locked the account out of BOTH auth modes.
    """

    async def test_update_preserves_credentials_and_identity_links(self):
        stored = _repo_user(
            account_kind="individual",
            username="alice",
            email="alice@example.com",
            hashed_password="$2b$12$bcrypt-hash",
            sso_provider="workos",
            sso_provider_id="wos_123",
            is_email_verified=True,
        )
        store, repo = _store(stored)
        user = await store.get_user("user-123")

        user.roles = ["user", "admin"]  # what promote_to_platform_admin.py does
        await store.update_user(user)

        written = repo.update.call_args.args[0]
        assert written.hashed_password == "$2b$12$bcrypt-hash"
        assert written.sso_provider == "workos"
        assert written.sso_provider_id == "wos_123"
        assert written.is_email_verified is True

    async def test_update_still_applies_the_fields_devuser_owns(self):
        store, repo = _store(_repo_user(account_kind="individual"))
        user = await store.get_user("user-123")

        user.roles = ["user", "admin"]
        user.display_name = "Renamed"
        user.is_active = False
        await store.update_user(user)

        written = repo.update.call_args.args[0]
        assert written.roles == ["user", "admin"]
        assert written.display_name == "Renamed"
        assert written.is_active is False
