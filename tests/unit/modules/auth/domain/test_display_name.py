"""No display name derived from an email address (#1924).

A display name is shown to people other than its owner (a shared case's creator
and driver, the driver-candidate list, a verified runbook's ``verified_by``).
Owner ruling: an account with no real name is labelled ``Member`` plus the first
four characters of its user id. These tests pin the one owner of that rule and
every writer of ``users.display_name`` that consults it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.modules.auth.domain.display_name import (
    MAX_DISPLAY_NAME_LENGTH,
    neutral_display_name,
    usable_display_name,
)

pytestmark = pytest.mark.unit


class TestUsableDisplayName:
    @pytest.mark.parametrize("raw", [None, "", " ", " \t\n ", 42, ["Alice"]])
    def test_no_name(self, raw):
        assert usable_display_name(raw) is None

    def test_strips(self):
        assert usable_display_name("  Alice Smith \n") == "Alice Smith"

    def test_caps_at_the_column_width(self):
        name = usable_display_name("A" * 250)
        assert name == "A" * MAX_DISPLAY_NAME_LENGTH
        assert MAX_DISPLAY_NAME_LENGTH == 200

    def test_a_cap_landing_in_whitespace_leaves_no_trailing_space(self):
        name = usable_display_name("A" * 199 + " " + "B" * 10)
        assert name == "A" * 199


class TestNeutralDisplayName:
    def test_member_and_four_characters_of_the_id(self):
        assert (
            neutral_display_name("3f9a1c2e-0000-4000-8000-000000000001")
            == "Member 3f9a"
        )

    def test_needs_an_id(self):
        with pytest.raises(ValueError):
            neutral_display_name("")


def _database_store() -> tuple[DatabaseUserStore, AsyncMock]:
    repo = AsyncMock()
    repo.get_by_username.return_value = None
    repo.get_by_email.return_value = None
    repo.save.side_effect = lambda user: user
    return DatabaseUserStore(repo), repo


def _redis_store() -> RedisUserStore:
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set = AsyncMock()
    redis.sadd = AsyncMock()
    return RedisUserStore(redis_client=redis)


@pytest.mark.asyncio
class TestDatabaseStoreRegistration:
    async def test_an_address_username_with_no_name_gets_the_neutral_label(self):
        """Before #1924 this stored ``Alice Smith@Example Com``."""
        store, repo = _database_store()

        user = await store.create_user(
            username="alice.smith@example.com",
            email="alice.smith@example.com",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        written = repo.save.call_args.args[0]
        assert written.display_name == f"Member {written.user_id[:4]}"
        assert user.display_name == written.display_name
        assert "alice" not in written.display_name.lower()

    async def test_a_plain_username_keeps_its_title_cased_form(self):
        store, repo = _database_store()

        await store.create_user(
            username="carol_x", enterprise_id=STANDALONE_ENTERPRISE_ID
        )

        assert repo.save.call_args.args[0].display_name == "Carol X"

    async def test_a_given_name_is_kept_stripped(self):
        store, repo = _database_store()

        await store.create_user(
            username="dan@example.com",
            email="dan@example.com",
            display_name="  Daniel  ",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        assert repo.save.call_args.args[0].display_name == "Daniel"

    async def test_a_whitespace_name_is_no_name(self):
        store, repo = _database_store()

        await store.create_user(
            username="dan@example.com",
            email="dan@example.com",
            display_name="   ",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        written = repo.save.call_args.args[0]
        assert written.display_name == f"Member {written.user_id[:4]}"


@pytest.mark.asyncio
class TestRedisStoreRegistration:
    async def test_an_address_username_with_no_name_gets_the_neutral_label(self):
        """Before #1924 this stored ``Alice Smith`` (the local part)."""
        user = await _redis_store().create_user(
            username="alice.smith@example.com",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        assert user.display_name == f"Member {user.user_id[:4]}"

    async def test_a_plain_username_keeps_its_title_cased_form(self):
        user = await _redis_store().create_user(
            username="carol_x-y", enterprise_id=STANDALONE_ENTERPRISE_ID
        )

        assert user.display_name == "Carol X Y"

    async def test_a_given_name_is_kept_stripped(self):
        user = await _redis_store().create_user(
            username="dan@example.com",
            display_name=" Daniel ",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        assert user.display_name == "Daniel"
