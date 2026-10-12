"""No display name derived from an email address (#1924).

A display name is shown to people other than its owner (a shared case's creator
and driver, the driver-candidate list, a verified runbook's ``verified_by``).
Owner ruling: an account with no real name is labelled ``Member`` plus the first
four characters of its user id. These tests pin the one owner of that rule and
every writer of ``users.display_name`` that consults it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis as fakeredis
import pytest

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.synthesized_email import (
    is_synthesized_email,
    synthesized_email,
)
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.modules.auth.domain.display_name import (
    MAX_DISPLAY_NAME_LENGTH,
    neutral_display_name,
    usable_display_name,
)
from faultmaven.modules.auth.domain.services.service_account_provisioning import (
    provision_service_account_credential,
)

pytestmark = pytest.mark.unit


class TestUsableDisplayName:
    @pytest.mark.parametrize("raw", [None, "", " ", " \t\n ", 42, ["Alice"]])
    def test_no_name(self, raw):
        assert usable_display_name(raw) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "a@b.com",
            "Name <a@b.com>",
            "a@b.com (x)",
            "  a@b.c ",
            "@",
        ],
    )
    def test_anything_holding_an_at_sign_is_no_name(self, raw):
        assert usable_display_name(raw) is None
        assert usable_display_name(raw, email="someone@else.com") is None

    @pytest.mark.parametrize("raw", ["sam.smith", "SAM.SMITH", "  Sam.Smith "])
    def test_the_emails_local_part_is_no_name(self, raw):
        assert usable_display_name(raw, email="sam.smith@example.com") is None

    def test_separators_are_not_normalised(self):
        """``Sam Smith`` is what a person types for sam.smith@, not a rendering
        of the address."""
        assert (
            usable_display_name("Sam Smith", email="sam.smith@example.com")
            == "Sam Smith"
        )

    def test_the_local_part_rule_needs_the_email(self):
        assert usable_display_name("sam.smith") == "sam.smith"

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


#: (username, email, display name stored with no name given). The generated
#: name passes the rule too, judged against a REAL email only (#1924).
GENERATED_NAMES = (
    ("alice", "alice@acme.com", None),  # "Alice" is the local part: neutral
    ("alice", None, "Alice"),  # synthesized email: the username is chosen
    ("alice.smith", "alice.smith@acme.com", "Alice Smith"),  # separators differ
    ("Bob", "bob@acme.com", None),  # the local part in another case: neutral
)


@pytest.mark.asyncio
@pytest.mark.parametrize("username,email,expected", GENERATED_NAMES)
async def test_the_database_stores_generated_name_passes_the_rule(
    username, email, expected
):
    store, repo = _database_store()

    await store.create_user(
        username=username, email=email, enterprise_id=STANDALONE_ENTERPRISE_ID
    )

    written = repo.save.call_args.args[0]
    assert written.display_name == (expected or f"Member {written.user_id[:4]}")


@pytest.mark.asyncio
@pytest.mark.parametrize("username,email,expected", GENERATED_NAMES)
async def test_the_redis_stores_generated_name_passes_the_rule(
    username, email, expected
):
    user = await _redis_store().create_user(
        username=username, email=email, enterprise_id=STANDALONE_ENTERPRISE_ID
    )

    assert user.display_name == (expected or f"Member {user.user_id[:4]}")


class TestSynthesizedEmail:
    def test_the_stores_placeholder_is_recognised(self):
        assert is_synthesized_email(synthesized_email("bob"))
        assert is_synthesized_email("Bob@FaultMaven.Example")

    @pytest.mark.parametrize("email", [None, "", "bob@acme.com", "bob@faultmaven.com"])
    def test_a_real_or_missing_email_is_not(self, email):
        assert not is_synthesized_email(email)


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

    @pytest.mark.parametrize("name", ["dan@example.com", "dan", "Dan <x@y.com>"])
    async def test_an_address_or_its_local_part_as_the_name_is_no_name(self, name):
        store, repo = _database_store()

        await store.create_user(
            username="dan@example.com",
            email="dan@example.com",
            display_name=name,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        written = repo.save.call_args.args[0]
        assert written.display_name == f"Member {written.user_id[:4]}"

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

    @pytest.mark.parametrize("name", ["dan@example.com", "Dan"])
    async def test_an_address_or_its_local_part_as_the_name_is_no_name(self, name):
        user = await _redis_store().create_user(
            username="dan@example.com",
            display_name=name,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        assert user.display_name == f"Member {user.user_id[:4]}"

    async def test_a_given_name_is_kept_stripped(self):
        user = await _redis_store().create_user(
            username="dan@example.com",
            display_name=" Daniel ",
            enterprise_id=STANDALONE_ENTERPRISE_ID,
        )

        assert user.display_name == "Daniel"


def _token_generator():
    async def generate_refresh_token(user, *, state_read_at):
        return "refresh"

    return SimpleNamespace(generate_refresh_token=generate_refresh_token)


def _saving_database_store() -> DatabaseUserStore:
    """A DatabaseUserStore over a repository that keeps what it saves, so
    ``update_user`` reads back what ``create_user`` wrote."""
    rows: dict = {}
    repo = AsyncMock()

    async def _save(user):
        rows[user.user_id] = user
        return user

    async def _get(user_id):
        return rows.get(user_id)

    async def _by_username(username):
        return next((u for u in rows.values() if u.username == username), None)

    repo.save.side_effect = _save
    repo.update.side_effect = _save
    repo.get.side_effect = _get
    repo.get_by_username.side_effect = _by_username
    repo.get_by_email.return_value = None
    return DatabaseUserStore(repo)


@pytest.mark.asyncio
class TestServiceAccountProvisioning:
    """The operator's service-account command writes a display name too."""

    async def test_a_plain_username_is_named_after_itself(self):
        store = _saving_database_store()

        credential = await provision_service_account_credential(
            username="slack-agent",
            user_store=store,
            token_generator=_token_generator(),
        )

        assert credential.user.display_name == "slack-agent (service account)"

    async def test_an_address_username_is_labelled_neutrally(self):
        """The Redis store: the database store cannot create an address
        username without an email (it would mint ``x@y.com@faultmaven.example``),
        and provisioning passes none."""
        store = RedisUserStore(redis_client=fakeredis.FakeRedis(decode_responses=True))

        credential = await provision_service_account_credential(
            username="bot@example.com",
            user_store=store,
            token_generator=_token_generator(),
        )

        user_id = credential.user.user_id
        assert credential.user.display_name == (
            f"Member {user_id[:4]} (service account)"
        )
        stored = await store.get_user(user_id)
        assert stored.display_name == credential.user.display_name
