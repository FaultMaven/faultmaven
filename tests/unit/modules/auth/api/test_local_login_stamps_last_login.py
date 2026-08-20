"""#1127: local login flows stamp ``users.last_login_at``.

#1123 made ``GET /auth/me`` report ``last_login`` from the stored user row —
but in the default ``AUTH_MODE=local`` deployment nothing ever wrote that
column (the only writers were the SSO path), so the endpoint faithfully
reported ``null`` forever. These pin the write side:

1. ``POST /auth/login`` (and ``/dev-login`` — same handler) stamps the row on
   successful authentication, and does NOT stamp on a failed one. RED if the
   ``record_login`` call is removed from ``local_login``.
2. ``POST /auth/register`` stamps too — it mints tokens, so it is the
   account's first login, matching the SSO JIT-create.
3. The stamp is best-effort: a store whose ``record_login`` raises must not
   fail the login (display metadata never outranks authentication).
4. ``DatabaseUserStore.record_login`` writes ``last_login_at`` (and
   ``updated_at``) onto the repository row — the row ``/auth/me`` reads —
   without touching any other column, and raises ``NotFoundError`` for a
   missing row.
5. ``RedisUserStore.record_login`` exists and accepts the call (deliberate
   no-op — DevUser persists no last-login and nothing reads one from Redis).
   The container picks the store at runtime, so a login handler calling a
   method only one store has would 500 under the Redis fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from faultmaven.config.settings import AuthMode
from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.infrastructure.persistence.user_repository import User as RepositoryUser
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.domain.models.api_auth import DevLoginRequest
from faultmaven.modules.auth.domain.models.auth import DevUser
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

USER_ID = "user-1127"
USERNAME = "operator"


def _dev_user() -> DevUser:
    return DevUser(
        user_id=USER_ID,
        username=USERNAME,
        email="operator@local.faultmaven",
        display_name="Operator",
        created_at=datetime.now(timezone.utc),
    )


class _RecordingUserStore:
    """User store double that records ``record_login`` calls."""

    def __init__(self, user: DevUser | None, record_error: Exception | None = None):
        self._user = user
        self._record_error = record_error
        self.recorded: list[str] = []

    async def get_user_by_username(self, username: str) -> DevUser | None:
        if self._user is not None and self._user.username == username:
            return self._user
        return None

    async def create_user(
        self, username: str, email=None, display_name=None
    ) -> DevUser:
        self._user = _dev_user()
        return self._user

    async def record_login(self, user_id: str) -> None:
        if self._record_error is not None:
            raise self._record_error
        self.recorded.append(user_id)


class _FakeSessionService:
    async def create_session(self, user_id: str, metadata: dict):
        return SimpleNamespace(session_id="session-1127")


class _StubTokenGenerator:
    async def generate_access_token(self, user, state_read_at=None) -> str:
        return "access-token-1127"

    async def generate_refresh_token(self, user, state_read_at=None) -> str:
        return "refresh-token-1127"


def _fake_request(user_store):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_store=user_store,
                token_revocation_store=InMemoryRevocationStore(),
            )
        )
    )


def _patches():
    settings_stub = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.LOCAL,
            jwt_access_token_expire_minutes=15,
        )
    )
    return (
        patch.object(
            auth_routes,
            "_build_local_jwt_generator",
            return_value=_StubTokenGenerator(),
        ),
        patch.object(auth_routes, "get_settings", return_value=settings_stub),
    )


async def _login(store) -> object:
    gen_patch, settings_patch = _patches()
    with gen_patch, settings_patch:
        return await auth_routes.local_login(
            DevLoginRequest(username=USERNAME),
            _fake_request(store),
            SimpleNamespace(headers={}),
            session_service=_FakeSessionService(),
        )


async def _register(store) -> object:
    gen_patch, settings_patch = _patches()
    with gen_patch, settings_patch:
        return await auth_routes.local_register(
            DevLoginRequest(username=USERNAME),
            _fake_request(store),
            SimpleNamespace(headers={}),
            session_service=_FakeSessionService(),
        )


# ---------------------------------------------------------------------------
# 1. The handlers stamp on success, and only on success
# ---------------------------------------------------------------------------


async def test_local_login_records_the_login():
    store = _RecordingUserStore(_dev_user())

    result = await _login(store)

    assert result.access_token == "access-token-1127"
    assert store.recorded == [USER_ID]


async def test_failed_login_records_nothing():
    store = _RecordingUserStore(user=None)  # unknown username -> 401

    with pytest.raises(HTTPException) as exc:
        await _login(store)

    assert exc.value.status_code == 401
    assert store.recorded == []


async def test_local_register_records_the_first_login():
    store = _RecordingUserStore(user=None)  # not yet registered

    result = await _register(store)

    assert result.access_token == "access-token-1127"
    assert store.recorded == [USER_ID]


# ---------------------------------------------------------------------------
# 2. Best-effort: a failing stamp must not fail the authentication
# ---------------------------------------------------------------------------


async def test_login_survives_a_failing_stamp():
    store = _RecordingUserStore(
        _dev_user(), record_error=RuntimeError("db blipped mid-stamp")
    )

    result = await _login(store)

    assert result.access_token == "access-token-1127"
    assert result.refresh_token == "refresh-token-1127"


# ---------------------------------------------------------------------------
# 3. DatabaseUserStore.record_login writes the row /auth/me reads
# ---------------------------------------------------------------------------


def _repository_user() -> RepositoryUser:
    created = datetime.now(timezone.utc) - timedelta(days=30)
    return RepositoryUser(
        user_id=USER_ID,
        username=USERNAME,
        email="operator@local.faultmaven",
        display_name="Operator",
        created_at=created,
        updated_at=created,
        last_login_at=None,
        roles=["user"],
    )


class _FakeUserRepository:
    def __init__(self, user: RepositoryUser | None):
        self._user = user
        self.updated: RepositoryUser | None = None

    async def get(self, user_id: str) -> RepositoryUser | None:
        if self._user is not None and self._user.user_id == user_id:
            return self._user
        return None

    async def update(self, user: RepositoryUser) -> RepositoryUser:
        self.updated = user
        return user


async def test_record_login_stamps_last_login_at_on_the_stored_row():
    row = _repository_user()
    repo = _FakeUserRepository(row)
    store = DatabaseUserStore(repo)

    before = datetime.now(timezone.utc)
    await store.record_login(USER_ID)
    after = datetime.now(timezone.utc)

    assert repo.updated is not None
    assert repo.updated.last_login_at is not None
    assert before <= repo.updated.last_login_at <= after
    assert repo.updated.updated_at == repo.updated.last_login_at
    # Only the timestamps move — everything else on the row is untouched.
    assert repo.updated.username == USERNAME
    assert repo.updated.roles == ["user"]
    assert repo.updated.created_at == row.created_at
    assert repo.updated.hashed_password is None


async def test_record_login_raises_not_found_for_a_missing_row():
    store = DatabaseUserStore(_FakeUserRepository(user=None))

    with pytest.raises(NotFoundError):
        await store.record_login("no-such-user")


# ---------------------------------------------------------------------------
# 4. RedisUserStore parity: the call must be accepted (as a no-op)
# ---------------------------------------------------------------------------


async def test_redis_user_store_accepts_record_login():
    store = RedisUserStore(redis_client=SimpleNamespace())

    # Must neither raise nor touch Redis — the client stub has no methods.
    await store.record_login(USER_ID)
