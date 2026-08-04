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
        session, _resumed = await service.create_session("user-1")

        assert session.expires_at is not None
        assert session.expires_at > datetime.now(timezone.utc)

    async def test_unexpired_session_is_returned_under_both_modes(self) -> None:
        service = _minimal_session_service()
        session, _resumed = await service.create_session("user-1")

        assert await service.get_session(session.session_id) is session
        assert await service.get_session(session.session_id, validate=False) is session
        assert session.session_id in service.sessions

    async def test_expired_session_with_validate_true_is_absent_and_removed(
        self,
    ) -> None:
        """validate=True mirrors AuthSessionService: expired == absent, and gone."""
        service = _minimal_session_service()
        session, _resumed = await service.create_session("user-1")
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
        session, _resumed = await service.create_session("user-1")
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        returned = await service.get_session(session.session_id, validate=False)

        assert returned is session
        assert service.sessions.get(session.session_id) is session, (
            "validate=False must have no delete side effect; the expired "
            "session must still be in the store afterwards"
        )

    async def test_validate_session_reports_expiry(self) -> None:
        service = _minimal_session_service()
        session, _resumed = await service.create_session("user-1")

        assert await service.validate_session(session.session_id) is True

        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await service.validate_session(session.session_id) is False

    async def test_unknown_session_is_none_under_both_modes(self) -> None:
        service = _minimal_session_service()

        assert await service.get_session("nope") is None
        assert await service.get_session("nope", validate=False) is None


@pytest.mark.unit
class TestMinimalSessionServiceCreateSession:
    """Rider D — create_session must be substitutable for the real service.

    ``sso_login_service`` does ``session, _resumed = await
    create_session(...)`` unconditionally, so a bare return value raised
    TypeError and 500'd SSO login whenever the degraded fallback was active.
    Return shape is NOT something the signature guard can see (it is
    signature-level only), which is why it is pinned here.
    """

    async def test_returns_session_and_resumed_pair(self) -> None:
        result = await _minimal_session_service().create_session("user-1")

        assert isinstance(result, tuple) and len(result) == 2
        session, resumed = result
        assert session.user_id == "user-1"
        assert resumed is False

    async def test_unpacks_the_way_sso_login_unpacks(self) -> None:
        """The exact call shape in sso_login_service.py must not raise."""
        session, _resumed = await _minimal_session_service().create_session(
            user_id="user-1",
            metadata={"login_method": "sso"},
        )
        assert getattr(session, "session_id", None)

    @pytest.mark.parametrize("bad", ["", "   ", None])
    async def test_blank_user_id_is_refused(self, bad) -> None:
        """Mirrors AuthSessionService, which raises rather than minting."""
        from faultmaven.exceptions import ValidationException

        with pytest.raises(ValidationException):
            await _minimal_session_service().create_session(bad)

    async def test_same_client_id_resumes_instead_of_minting(self) -> None:
        service = _minimal_session_service()
        first, resumed_first = await service.create_session("user-1", client_id="dev-a")
        second, resumed_second = await service.create_session(
            "user-1", client_id="dev-a"
        )

        assert resumed_first is False
        assert resumed_second is True
        assert second.session_id == first.session_id
        assert (
            len(service.sessions) == 1
        ), "resumption must not leave a second session in the store"

    async def test_different_client_id_mints_a_new_session(self) -> None:
        service = _minimal_session_service()
        first, _ = await service.create_session("user-1", client_id="dev-a")
        second, resumed = await service.create_session("user-1", client_id="dev-b")

        assert resumed is False
        assert second.session_id != first.session_id
        assert len(service.sessions) == 2


@pytest.mark.unit
class TestMinimalSessionServiceUpdateLastActivity:
    """Rider C — heartbeat must honour expiry the way the real service does."""

    async def test_live_session_is_touched(self) -> None:
        service = _minimal_session_service()
        session, _ = await service.create_session("user-1")
        before = session.last_activity

        assert await service.update_last_activity(session.session_id) is True
        assert session.last_activity >= before
        assert session.session_id in service.sessions

    async def test_expired_session_is_refused_and_evicted(self) -> None:
        """Expired heartbeat answers False and does not resurrect the session.

        The store assertion is the point: an implementation that returned
        False but left the dead session in place would still drift from the
        real service, whose get_session deletes on expiry.
        """
        service = _minimal_session_service()
        session, _ = await service.create_session("user-1")
        session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        assert await service.update_last_activity(session.session_id) is False
        assert session.session_id not in service.sessions

    async def test_unknown_session_is_refused(self) -> None:
        assert await _minimal_session_service().update_last_activity("nope") is False
