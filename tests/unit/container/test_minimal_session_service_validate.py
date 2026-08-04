"""``MinimalSessionService.get_session`` must honour ``validate``.

This stand-in is not test-only scaffolding: ``create_session_service``
(``container/providers/services.py``) returns it in PRODUCTION whenever the
session store is unavailable (Redis down). Before this test it accepted
``validate`` and discarded it, so a degraded deployment happily handed out
expired sessions — and, in the other direction, a non-validating read must not
delete what it reads (the heartbeat endpoint observes ``last_activity``
through ``validate=False``).

The store-contents assertions are the point: checking only the return value
lets a "returns the right object but deletes it anyway" implementation pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _minimal_session_service():
    """A stand-in built off a bare container shell.

    ``DIContainer.__new__`` returns SESSION STATE (the process singleton), so
    ``object.__new__`` is used to avoid touching it.
    """
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)._create_minimal_session_service()


@pytest.mark.unit
class TestMinimalSessionServiceValidate:
    async def test_created_session_carries_an_expiry(self) -> None:
        """Without expires_at, validate=True could never detect expiry."""
        service = _minimal_session_service()
        session = await service.create_session("user-1")

        assert session.expires_at is not None
        assert session.expires_at > datetime.now(timezone.utc)

    async def test_unexpired_session_is_returned_under_both_modes(self) -> None:
        service = _minimal_session_service()
        session = await service.create_session("user-1")

        assert await service.get_session(session.session_id) is session
        assert await service.get_session(session.session_id, validate=False) is session
        assert session.session_id in service.sessions

    async def test_expired_session_with_validate_true_is_absent_and_removed(
        self,
    ) -> None:
        """validate=True mirrors AuthSessionService: expired == absent, and gone."""
        service = _minimal_session_service()
        session = await service.create_session("user-1")
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        assert await service.get_session(session.session_id) is None
        assert session.session_id not in service.sessions, (
            "validate=True must delete the expired session, mirroring "
            "AuthSessionService.get_session"
        )

    async def test_expired_session_with_validate_false_is_returned_and_kept(
        self,
    ) -> None:
        """validate=False must not check expiry AND must not delete.

        The store assertion is what kills the mutant that returns the session
        and deletes it anyway — a read must never destroy what it reads.
        """
        service = _minimal_session_service()
        session = await service.create_session("user-1")
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        returned = await service.get_session(session.session_id, validate=False)

        assert returned is session
        assert service.sessions.get(session.session_id) is session, (
            "validate=False must have no delete side effect; the expired "
            "session must still be in the store afterwards"
        )

    async def test_validate_session_reports_expiry(self) -> None:
        service = _minimal_session_service()
        session = await service.create_session("user-1")

        assert await service.validate_session(session.session_id) is True

        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await service.validate_session(session.session_id) is False

    async def test_unknown_session_is_none_under_both_modes(self) -> None:
        service = _minimal_session_service()

        assert await service.get_session("nope") is None
        assert await service.get_session("nope", validate=False) is None
