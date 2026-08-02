"""Unit tests for AuthSessionService.get_session(validate=...)

The published contract `ISessionService.get_session` (modules/auth/contracts.py)
declares `validate: bool = True`, and five call sites pass the kwarg. The concrete
service did not accept it, so every validating caller raised

    TypeError: AuthSessionService.get_session() got an unexpected keyword argument 'validate'

which surfaced as an unconditional HTTP 500 on
`POST /api/v1/cases/sessions/{session_id}/case`, and as a silently swallowed
fallback in the heartbeat endpoint.

`validate` controls expiry enforcement:
- validate=True (default): an expired session is deleted and None is returned.
- validate=False: the stored session is returned as-is — no expiry check and,
  critically, NO delete side effect. A read must not destroy a session.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.models import SessionContext
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)

# ============================================================
# Test Fixtures
# ============================================================


class InMemorySessionStore:
    """Minimal session store honouring the surface AuthSessionService uses.

    Mirrors RedisSessionStore: `get_session(session_id, validate=True)` performs no
    expiry enforcement of its own (that is the service's job), and `delete` removes
    the row and reports whether anything was removed.
    """

    def __init__(self, *sessions: SessionContext):
        self._sessions = {s.session_id: s for s in sessions}
        self.delete_calls: list[str] = []

    async def get_session(self, session_id, validate=True):
        return self._sessions.get(session_id)

    async def delete(self, session_id):
        self.delete_calls.append(session_id)
        return self._sessions.pop(session_id, None) is not None

    def contains(self, session_id) -> bool:
        return session_id in self._sessions


def make_session(session_id: str = "sess-1", *, expired: bool) -> SessionContext:
    now = datetime.now(timezone.utc)
    return SessionContext(
        session_id=session_id,
        user_id="user-1",
        created_at=now - timedelta(hours=2),
        last_activity=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
        expires_at=(
            (now - timedelta(hours=1)) if expired else (now + timedelta(hours=1))
        ),
    )


@pytest.fixture
def live_session() -> SessionContext:
    return make_session(expired=False)


@pytest.fixture
def expired_session() -> SessionContext:
    return make_session(expired=True)


# ============================================================
# Live session: validate=True / validate=False / default
# ============================================================


@pytest.mark.unit
@pytest.mark.session
async def test_live_session_with_validate_true_returns_session(live_session):
    """The case-module call site (`validate=True`) must not raise and must return."""
    store = InMemorySessionStore(live_session)
    service = AuthSessionService(session_store=store)

    result = await service.get_session("sess-1", validate=True)

    assert result is not None
    assert result.session_id == "sess-1"
    assert store.delete_calls == []


@pytest.mark.unit
@pytest.mark.session
async def test_live_session_with_validate_false_returns_session(live_session):
    """The heartbeat call site (`validate=False`) must not raise and must return."""
    store = InMemorySessionStore(live_session)
    service = AuthSessionService(session_store=store)

    result = await service.get_session("sess-1", validate=False)

    assert result is not None
    assert result.session_id == "sess-1"
    assert store.delete_calls == []


@pytest.mark.unit
@pytest.mark.session
async def test_default_matches_validate_true_on_live_session(live_session):
    """Omitting `validate` must behave exactly as validate=True (no behaviour change)."""
    default_store = InMemorySessionStore(make_session(expired=False))
    explicit_store = InMemorySessionStore(make_session(expired=False))

    default_result = await AuthSessionService(session_store=default_store).get_session(
        "sess-1"
    )
    explicit_result = await AuthSessionService(
        session_store=explicit_store
    ).get_session("sess-1", validate=True)

    assert default_result is not None
    assert explicit_result is not None
    assert default_result.session_id == explicit_result.session_id
    assert default_store.delete_calls == explicit_store.delete_calls == []


# ============================================================
# Expired session: the load-bearing difference
# ============================================================


@pytest.mark.unit
@pytest.mark.session
async def test_expired_session_with_validate_true_returns_none_and_deletes(
    expired_session,
):
    """validate=True enforces expiry: None returned AND the session is deleted."""
    store = InMemorySessionStore(expired_session)
    service = AuthSessionService(session_store=store)

    result = await service.get_session("sess-1", validate=True)

    assert result is None
    assert store.delete_calls == ["sess-1"]
    assert not store.contains("sess-1")


@pytest.mark.unit
@pytest.mark.session
async def test_expired_session_with_validate_false_returns_session_and_does_not_delete(
    expired_session,
):
    """validate=False is a pure read.

    The load-bearing property: reading an expired session must NOT destroy it.
    Asserting the returned value alone would still pass if the service deleted the
    row and handed back the in-flight object, so assert the store's contents.
    """
    store = InMemorySessionStore(expired_session)
    service = AuthSessionService(session_store=store)

    result = await service.get_session("sess-1", validate=False)

    assert result is not None
    assert result.session_id == "sess-1"
    assert result.last_activity == expired_session.last_activity
    assert store.delete_calls == []
    assert store.contains("sess-1")

    # And the session is still readable afterwards — the read was non-destructive.
    again = await service.get_session("sess-1", validate=False)
    assert again is not None
    assert again.session_id == "sess-1"


@pytest.mark.unit
@pytest.mark.session
async def test_expired_session_default_returns_none_and_deletes(expired_session):
    """Omitting `validate` on an expired session preserves the pre-fix behaviour."""
    store = InMemorySessionStore(expired_session)
    service = AuthSessionService(session_store=store)

    result = await service.get_session("sess-1")

    assert result is None
    assert store.delete_calls == ["sess-1"]
    assert not store.contains("sess-1")


# ============================================================
# Callers that must keep validating
# ============================================================


@pytest.mark.unit
@pytest.mark.session
async def test_validate_session_still_enforces_expiry(expired_session):
    """validate_session() delegates with the default and must stay validating."""
    store = InMemorySessionStore(expired_session)
    service = AuthSessionService(session_store=store)

    assert await service.validate_session("sess-1") is False
    assert store.delete_calls == ["sess-1"]


@pytest.mark.unit
@pytest.mark.session
async def test_get_user_from_session_still_enforces_expiry(expired_session):
    """get_user_from_session() must not hand out a user_id from an expired session."""
    store = InMemorySessionStore(expired_session)
    service = AuthSessionService(session_store=store)

    assert await service.get_user_from_session("sess-1") is None
    assert store.delete_calls == ["sess-1"]


@pytest.mark.unit
@pytest.mark.session
async def test_get_user_from_session_returns_user_for_live_session(live_session):
    store = InMemorySessionStore(live_session)
    service = AuthSessionService(session_store=store)

    assert await service.get_user_from_session("sess-1") == "user-1"


# ============================================================
# Contract conformance
# ============================================================


@pytest.mark.unit
@pytest.mark.session
async def test_missing_session_returns_none_under_both_modes():
    store = InMemorySessionStore()
    service = AuthSessionService(session_store=store)

    assert await service.get_session("nope", validate=True) is None
    assert await service.get_session("nope", validate=False) is None
    assert store.delete_calls == []


@pytest.mark.unit
@pytest.mark.session
async def test_service_satisfies_isessionservice_get_session_signature():
    """The concrete service must accept the kwarg its own contract publishes."""
    import inspect

    from faultmaven.modules.auth.contracts import ISessionService

    contract_param = inspect.signature(ISessionService.get_session).parameters[
        "validate"
    ]
    service_param = inspect.signature(AuthSessionService.get_session).parameters[
        "validate"
    ]

    assert service_param.default == contract_param.default is True
