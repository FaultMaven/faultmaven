"""#1127: local login flows stamp ``users.last_login_at``.

#1123 made ``GET /auth/me`` report ``last_login`` from the stored user row —
but in the default ``AUTH_MODE=local`` deployment nothing ever wrote that
column (the only writers were the SSO path), so the endpoint faithfully
reported ``null`` forever. These pin the write side, as reshaped by the PR
review (the first cut was a full-row read-modify-write; see #1130):

1. ``POST /auth/login`` (and ``/dev-login`` — same handler) stamps the row on
   successful authentication, and does NOT stamp on a failed one or for a
   deactivated/soft-deleted account (fresh login metadata on a disabled
   account would read as the deactivation not holding). RED if the
   ``record_login`` call or its gate is removed from ``local_login``.
2. ``POST /auth/register`` stamps too — it mints tokens, so it is the
   account's first login, matching the SSO JIT-create.
3. The stamp is best-effort: a store whose ``record_login`` raises must not
   fail the login (display metadata never outranks authentication), and a
   store *lacking* the method entirely — the stores are duck-typed, nothing
   enforces the surface — must not fail the login either, but is logged as an
   ERROR: that is a wiring defect that silently reproduces #1127.
4. ``DatabaseUserStore.record_login`` delegates to the repository's
   ``touch_last_login`` — a targeted single-column write, never
   ``get()``+``update()``, which upserts every column and can revert a
   concurrent deactivation or password reset — and raises ``NotFoundError``
   when the row is missing.
5. ``UserRepository.touch_last_login`` (in-memory reference implementation)
   moves ``last_login_at`` and nothing else — in particular NOT
   ``updated_at``, which keeps meaning "last material change to the account"
   rather than becoming a shadow of the login clock.
6. ``RedisUserStore.record_login`` accepts the call as a deliberate no-op
   (DevUser persists no last-login and nothing reads one from Redis), and
   warns once per process on first use: the container falls back to this
   store when DatabaseUserStore construction fails, a state in which logins
   would otherwise silently stop stamping while /auth/me still reads DB rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from faultmaven.config.settings import AuthMode
from faultmaven.exceptions import NotFoundError
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    PostgreSQLUserRepository,
)
from faultmaven.infrastructure.persistence.user_repository import User as RepositoryUser
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.domain.models.api_auth import DevLoginRequest
from faultmaven.modules.auth.domain.models.auth import DevUser
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

USER_ID = "user-1127"
USERNAME = "operator"


def _dev_user(is_active: bool = True) -> DevUser:
    return DevUser(
        user_id=USER_ID,
        username=USERNAME,
        email="operator@local.faultmaven",
        display_name="Operator",
        created_at=datetime.now(timezone.utc),
        is_active=is_active,
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


class _StoreWithoutRecordLogin:
    """A store that never grew ``record_login`` — the duck-typing hazard."""

    def __init__(self, user: DevUser):
        self._user = user

    async def get_user_by_username(self, username: str) -> DevUser | None:
        if self._user.username == username:
            return self._user
        return None


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
# 1. The handlers stamp on success, and only for a usable account
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


async def test_deactivated_account_login_records_nothing():
    """Same gate as the revocation-watermark clear: a disabled account must
    not gain fresh login metadata (an admin reading "last login: just now" on
    it would conclude the deactivation isn't holding)."""
    store = _RecordingUserStore(_dev_user(is_active=False))

    await _login(store)

    assert store.recorded == []


async def test_local_register_records_the_first_login():
    store = _RecordingUserStore(user=None)  # not yet registered

    result = await _register(store)

    assert result.access_token == "access-token-1127"
    assert store.recorded == [USER_ID]


# ---------------------------------------------------------------------------
# 2. Best-effort: neither a failing stamp nor a missing method fails the login
# ---------------------------------------------------------------------------


async def test_login_survives_a_failing_stamp():
    store = _RecordingUserStore(
        _dev_user(), record_error=RuntimeError("db blipped mid-stamp")
    )

    result = await _login(store)

    assert result.access_token == "access-token-1127"
    assert result.refresh_token == "refresh-token-1127"


async def test_login_survives_a_store_without_record_login_and_logs_error(caplog):
    """The stores are duck-typed; a store lacking the method is a wiring
    defect that would silently reproduce #1127. The login must still succeed,
    but the miss is an ERROR, not a per-login warning lost in the noise."""
    store = _StoreWithoutRecordLogin(_dev_user())

    with caplog.at_level(logging.ERROR, logger=auth_routes.logger.name):
        result = await _login(store)

    assert result.access_token == "access-token-1127"
    assert any(
        "no record_login" in record.message and record.levelno == logging.ERROR
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 3. DatabaseUserStore.record_login is a targeted stamp, not a row rewrite
# ---------------------------------------------------------------------------


class _FakeUserRepository:
    """Repository double exposing only the targeted-stamp surface.

    Deliberately has no ``get``/``update``: if record_login regresses to a
    read-modify-write (the racy shape the #1130 review rejected), these tests
    fail with AttributeError rather than passing on a fake that can't model
    the race.
    """

    def __init__(self, existing: bool):
        self._existing = existing
        self.touched: list[tuple[str, datetime]] = []

    async def touch_last_login(self, user_id: str, at: datetime) -> bool:
        if not self._existing:
            return False
        self.touched.append((user_id, at))
        return True


async def test_record_login_touches_last_login_via_the_targeted_write():
    repo = _FakeUserRepository(existing=True)
    store = DatabaseUserStore(repo)

    before = datetime.now(timezone.utc)
    await store.record_login(USER_ID)
    after = datetime.now(timezone.utc)

    assert len(repo.touched) == 1
    touched_id, touched_at = repo.touched[0]
    assert touched_id == USER_ID
    assert before <= touched_at <= after
    assert touched_at.tzinfo is not None


async def test_record_login_raises_not_found_for_a_missing_row():
    store = DatabaseUserStore(_FakeUserRepository(existing=False))

    with pytest.raises(NotFoundError):
        await store.record_login("no-such-user")


# ---------------------------------------------------------------------------
# 4. touch_last_login moves last_login_at and NOTHING else
# ---------------------------------------------------------------------------


async def test_touch_last_login_moves_only_last_login_at():
    """Reference semantics, on the in-memory implementation: the stamp does
    not bump ``updated_at`` (a login is not an account modification — the
    admin-visible "last modified" must not become a shadow of the login
    clock) and touches no other column."""
    created = datetime.now(timezone.utc) - timedelta(days=30)
    repo = InMemoryUserRepository()
    await repo.create(
        RepositoryUser(
            user_id=USER_ID,
            username=USERNAME,
            email="operator@local.faultmaven",
            display_name="Operator",
            created_at=created,
            updated_at=created,
            last_login_at=None,
            roles=["user"],
        )
    )
    # Snapshot AFTER create: save() re-stamps updated_at on write, and the
    # invariant under test is that touch_last_login doesn't move it further.
    stored = await repo.get(USER_ID)
    baseline = stored.model_copy(deep=True)

    stamp = datetime.now(timezone.utc)
    assert await repo.touch_last_login(USER_ID, stamp) is True

    stamped = await repo.get(USER_ID)
    assert stamped.last_login_at == stamp
    assert stamped.updated_at == baseline.updated_at
    assert stamped.created_at == baseline.created_at
    assert stamped.username == baseline.username
    assert stamped.roles == baseline.roles
    assert stamped.hashed_password == baseline.hashed_password
    assert stamped.is_active == baseline.is_active


async def test_touch_last_login_reports_a_missing_row():
    assert (
        await InMemoryUserRepository().touch_last_login(
            "no-such-user", datetime.now(timezone.utc)
        )
        is False
    )


# ---------------------------------------------------------------------------
# 4b. The same invariant against real SQLAlchemy: onupdate must be suppressed
# ---------------------------------------------------------------------------
#
# ``UserModel.updated_at`` carries ``onupdate=func.now()``, which fires on any
# UPDATE that doesn't set the column itself — so "just omit updated_at from
# SET" silently bumps it anyway, and no repository fake can catch that. This
# runs the real statement through a real engine; RED if the explicit
# self-assignment in touch_last_login is dropped.


def _utc(dt: datetime) -> datetime:
    """SQLite hands naive datetimes back; compare in UTC either way."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def test_touch_last_login_suppresses_the_updated_at_onupdate():
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            repo = PostgreSQLUserRepository(session)
            created = datetime.now(timezone.utc) - timedelta(days=30)
            await repo.save(
                RepositoryUser(
                    user_id=USER_ID,
                    username=USERNAME,
                    email="operator@local.faultmaven",
                    display_name="Operator",
                    created_at=created,
                    updated_at=created,
                    last_login_at=None,
                    roles=["user"],
                )
            )
            baseline = await repo.get(USER_ID)  # save() re-stamped updated_at

            stamp = datetime.now(timezone.utc) + timedelta(seconds=5)
            assert await repo.touch_last_login(USER_ID, stamp) is True

            row = await repo.get(USER_ID)
            assert _utc(row.last_login_at) == stamp
            assert row.updated_at == baseline.updated_at
            assert row.created_at == baseline.created_at
            assert row.is_active == baseline.is_active

            assert (
                await repo.touch_last_login("no-such-user", datetime.now(timezone.utc))
                is False
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. RedisUserStore parity: accepted as a no-op, warned once per process
# ---------------------------------------------------------------------------


async def test_redis_user_store_accepts_record_login_and_warns_once(caplog):
    # The client stub has no methods — a stamp that touched Redis would raise.
    store = RedisUserStore(redis_client=SimpleNamespace())

    with caplog.at_level(logging.WARNING):
        await store.record_login(USER_ID)
        await store.record_login(USER_ID)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "no-op on RedisUserStore" in r.message
    ]
    assert len(warnings) == 1
