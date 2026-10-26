"""A configured service that cannot be built is not replaced by a stand-in (#2103).

The case, session and auth factories caught any construction error and returned
another implementation: cases or sessions in process memory, or an
``AuthService`` with no revocation store. The error now propagates. The
in-memory stand-ins remain for the not-configured branch only.
"""

from unittest.mock import MagicMock, patch

import fakeredis.aioredis as fakeredis_aio
import pytest

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container.providers import services
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

pytestmark = pytest.mark.unit


def _boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def _no_stand_in():
    pytest.fail("a configured service was replaced by its in-memory stand-in")


@pytest.fixture
def settings():
    return FaultMavenSettings(_env_file=None)


def _session_store():
    return RedisSessionStore(fakeredis_aio.FakeRedis(), session_ttl_seconds=1800)


def test_a_case_service_that_cannot_be_built_propagates(settings, monkeypatch):
    monkeypatch.setattr(CaseService, "__init__", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        services.create_case_service(
            InMemoryCaseRepository(),
            _session_store(),
            None,
            settings,
            _no_stand_in,
            file_storage_service=MagicMock(),
            redis_client=MagicMock(),
        )


def test_a_session_service_that_cannot_be_built_propagates(settings, monkeypatch):
    monkeypatch.setattr(AuthSessionService, "__init__", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        services.create_session_service(_session_store(), settings, _no_stand_in)


def test_an_auth_service_that_cannot_be_built_propagates(settings, monkeypatch):
    """Only the configured call raises, so the bare ``AuthService()`` the
    factory used to fall back to would still build, and fail this test."""
    original = AuthService.__init__

    def _refuse_the_configured_call(self, revocation_store=None, **kwargs):
        if revocation_store is not None:
            raise RuntimeError("boom")
        original(self, revocation_store=None, **kwargs)

    monkeypatch.setattr(AuthService, "__init__", _refuse_the_configured_call)
    with pytest.raises(RuntimeError, match="boom"):
        services.create_auth_service(MagicMock(name="revocation_store"), settings)


def test_an_auth_service_with_no_revocation_store_configured_is_built(settings):
    """The positive control: "not configured" is not a construction failure."""
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        service = services.create_auth_service(None, settings)
    assert isinstance(service, AuthService)
    assert service._revocation_store is None


def test_no_repository_or_store_still_answers_with_the_stand_in(settings):
    stand_in = object()
    assert (
        services.create_case_service(None, None, None, settings, lambda: stand_in)
        is stand_in
    )
    assert services.create_session_service(None, settings, lambda: stand_in) is stand_in
