"""Tests for typed exception contract in the user-store layer.

Item 3 in the 2026-05-20 investigation-pipeline-followups handoff.

Before this refactor, both ``DatabaseUserStore`` and the Redis-backed
``RedisUserStore`` (a.k.a. ``user_store.py``) raised raw ``ValueError``
for all error shapes — validation failures, conflicts, and not-found
all looked identical from the caller's perspective. Route layers then
caught the ``ValueError`` and returned HTTP 400 indiscriminately.

The refactor maps each error shape to a typed FaultMaven exception:

  - Invalid username/email format    → ``ValidationException`` (HTTP 422)
  - Username/email already exists    → ``ConflictError``       (HTTP 409)
  - User not found on update         → ``NotFoundError``       (HTTP 404)

Routes no longer need explicit ``except ValueError`` blocks; the typed
exceptions propagate to FastAPI's global handlers in
``api/exception_handlers.py``.

These tests pin the exception type AND the carried metadata
(``resource_type``, ``conflict_reason``, etc.) so a future regression
that drops the type information or reverts to ``ValueError`` fails
loudly.
"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.modules.auth.domain.models.auth import DevUser

# ---------------------------------------------------------------------------
# DatabaseUserStore
# ---------------------------------------------------------------------------


def _make_db_store_with_repo(
    existing_username: str | None = None, existing_email: str | None = None
) -> DatabaseUserStore:
    """Build a DatabaseUserStore backed by an AsyncMock UserRepository.

    The mock returns a stub user (truthy) for ``get_by_username`` /
    ``get_by_email`` calls that match the seeded fixtures, and None
    otherwise. Used to simulate "already exists" duplicate states.
    """
    # User model used by the SQLAlchemy-backed repository — defined in
    # faultmaven.infrastructure.persistence.user_repository, NOT the
    # auth-domain User dataclass which has a different shape.
    from datetime import datetime, timezone

    from faultmaven.infrastructure.persistence.user_repository import User as _User

    def _stub_user(username: str, email: str) -> _User:
        return _User(
            user_id="user_existing0",
            username=username,
            email=email,
            display_name=username,
            avatar_url=None,
            timezone="UTC",
            locale="en-US",
            hashed_password=None,
            is_active=True,
            is_email_verified=False,
            email_verified_at=None,
            sso_provider=None,
            sso_provider_id=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            last_login_at=None,
            last_password_change_at=None,
            deleted_at=None,
            roles=["user"],
        )

    async def _get_by_username(u: str):
        return _stub_user(u, f"{u}@x.example") if u == existing_username else None

    async def _get_by_email(e: str):
        return _stub_user(existing_username or "x", e) if e == existing_email else None

    async def _get(_id: str):
        return None  # never found unless overridden by test

    repo = AsyncMock()
    repo.get = _get
    repo.get_by_username = _get_by_username
    repo.get_by_email = _get_by_email
    repo.save = AsyncMock(side_effect=lambda u: u)
    repo.update = AsyncMock(side_effect=lambda u: u)
    return DatabaseUserStore(repo)


@pytest.mark.unit
@pytest.mark.asyncio
class TestDatabaseUserStoreTypedExceptions:
    """Pins the typed exception contract for DatabaseUserStore."""

    async def test_invalid_username_format_raises_validation_exception(self):
        store = _make_db_store_with_repo()
        with pytest.raises(ValidationException) as exc:
            await store.create_user(username="bad username with spaces")
        assert "Invalid username format" in str(exc.value)

    async def test_invalid_email_format_raises_validation_exception(self):
        store = _make_db_store_with_repo()
        with pytest.raises(ValidationException) as exc:
            await store.create_user(username="alice", email="not-an-email")
        assert "Invalid email format" in str(exc.value)

    async def test_duplicate_username_raises_conflict_error(self):
        store = _make_db_store_with_repo(existing_username="alice")
        with pytest.raises(ConflictError) as exc:
            await store.create_user(username="alice")
        # Carry metadata so the response can include actionable detail.
        assert exc.value.resource_type == "user"
        assert exc.value.resource_id == "alice"
        assert exc.value.conflict_reason == "duplicate_username"

    async def test_duplicate_email_raises_conflict_error(self):
        store = _make_db_store_with_repo(existing_email="alice@x.example")
        with pytest.raises(ConflictError) as exc:
            await store.create_user(username="bob", email="alice@x.example")
        assert exc.value.conflict_reason == "duplicate_email"

    async def test_update_user_not_found_raises_not_found_error(self):
        store = _make_db_store_with_repo()
        # repo.get returns None for any user_id — simulates missing user.
        dev_user = DevUser(
            user_id="user_unknown000",
            username="ghost",
            email="ghost@x.example",
            display_name="Ghost",
            created_at=None,
            is_dev_user=True,
            is_active=True,
        )
        with pytest.raises(NotFoundError) as exc:
            await store.update_user(dev_user)
        assert exc.value.resource_type == "user"
        assert exc.value.resource_id == "user_unknown000"


# ---------------------------------------------------------------------------
# RedisUserStore (FakeRedis-backed in tests)
# ---------------------------------------------------------------------------


def _make_redis_store() -> RedisUserStore:
    """Build a RedisUserStore with an in-memory FakeRedis. Sufficient
    for exercising the create_user / update_user code paths."""
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisUserStore(redis_client=fake)


@pytest.mark.unit
@pytest.mark.asyncio
class TestRedisUserStoreTypedExceptions:
    """Pins the typed exception contract for RedisUserStore (the
    in-memory fallback path used when no SQL backend is configured)."""

    async def test_invalid_username_format_raises_validation_exception(self):
        store = _make_redis_store()
        with pytest.raises(ValidationException) as exc:
            await store.create_user(username="bad username with spaces")
        assert "Invalid username format" in str(exc.value)

    async def test_invalid_email_format_raises_validation_exception(self):
        store = _make_redis_store()
        with pytest.raises(ValidationException) as exc:
            await store.create_user(username="alice", email="not-an-email")
        assert "Invalid email format" in str(exc.value)

    async def test_duplicate_username_raises_conflict_error(self):
        store = _make_redis_store()
        await store.create_user(username="alice")
        with pytest.raises(ConflictError) as exc:
            await store.create_user(username="alice")
        assert exc.value.conflict_reason == "duplicate_username"

    async def test_duplicate_email_raises_conflict_error(self):
        store = _make_redis_store()
        await store.create_user(username="alice", email="shared@x.example")
        with pytest.raises(ConflictError) as exc:
            await store.create_user(username="bob", email="shared@x.example")
        assert exc.value.conflict_reason == "duplicate_email"

    async def test_update_user_not_found_raises_not_found_error(self):
        store = _make_redis_store()
        ghost = DevUser(
            user_id="user_unknown000",
            username="ghost",
            email="ghost@x.example",
            display_name="Ghost",
            created_at=None,
            is_dev_user=True,
            is_active=True,
        )
        with pytest.raises(NotFoundError) as exc:
            await store.update_user(ghost)
        assert exc.value.resource_type == "user"
