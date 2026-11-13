"""A configured service that cannot be built is not replaced by a stand-in (#2103).

The case, session and auth factories caught any construction error and returned
another implementation: cases or sessions in process memory, or an
``AuthService`` with no revocation store. The case repository's factory
returned None, which read as "not configured". Each now raises
``RequiredServiceConstructionError`` naming the cause. The in-memory session
stand-in remains for the not-configured branch only; the case service has no
stand-in at all (#2548): ``create_case_repository`` never returns None, so a
case service asked to be built over no repository is refused the same way. At
request time, a missing case service answers 503, not a 401 that blames the
caller.
"""

from unittest.mock import MagicMock, patch

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import HTTPException

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.container.providers import infrastructure, services
from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.case.api.routes.dependencies import (
    check_case_service_available,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)

pytestmark = pytest.mark.unit

BOOM = RuntimeError("boom")


def _boom(*_args, **_kwargs):
    raise BOOM


@pytest.fixture
def settings():
    return FaultMavenSettings(_env_file=None)


def _session_store():
    return RedisSessionStore(fakeredis_aio.FakeRedis(), session_ttl_seconds=1800)


def _refused(build, name):
    with pytest.raises(RequiredServiceConstructionError) as refused:
        build()
    assert refused.value.__cause__ is BOOM
    assert str(refused.value) == f"{name} could not be constructed: boom"


def test_a_case_repository_that_cannot_be_built_is_refused(settings, monkeypatch):
    monkeypatch.setattr(SessionlessCaseRepository, "__init__", _boom)
    _refused(lambda: infrastructure.create_case_repository(settings), "Case repository")


def test_a_case_service_that_cannot_be_built_is_refused(settings, monkeypatch):
    monkeypatch.setattr(CaseService, "__init__", _boom)
    _refused(
        lambda: services.create_case_service(
            InMemoryCaseRepository(),
            _session_store(),
            None,
            settings,
            file_storage_service=MagicMock(),
            redis_client=MagicMock(),
        ),
        "CaseService",
    )


def test_a_session_service_that_cannot_be_built_is_refused(settings, monkeypatch):
    monkeypatch.setattr(AuthSessionService, "__init__", _boom)
    _refused(
        lambda: services.create_session_service(_session_store(), settings),
        "AuthSessionService",
    )


def test_an_auth_service_that_cannot_be_built_is_refused(settings, monkeypatch):
    monkeypatch.setattr(AuthService, "__init__", _boom)
    _refused(
        lambda: services.create_auth_service(MagicMock(name="revocation"), settings),
        "AuthService",
    )


def test_an_auth_service_with_no_revocation_store_configured_is_built(settings):
    """The positive control: "not configured" is not a construction failure."""
    with patch(
        "faultmaven.modules.auth.domain.services.auth_service.get_settings",
        return_value=settings,
    ):
        service = services.create_auth_service(None, settings)
    assert isinstance(service, AuthService)
    assert service._revocation_store is None


def test_no_session_store_is_refused_not_stood_in_for(settings):
    """No session service exists without a session store (#1855)."""
    with pytest.raises(RequiredServiceConstructionError) as refused:
        services.create_session_service(None, settings)
    assert str(refused.value) == (
        "AuthSessionService could not be constructed: no session store"
    )


def test_no_case_repository_is_refused_not_stood_in_for(settings):
    """No case service exists without a case repository (#2548)."""
    with pytest.raises(RequiredServiceConstructionError) as refused:
        services.create_case_service(None, _session_store(), None, settings)
    assert str(refused.value) == (
        "CaseService could not be constructed: no case repository"
    )


def test_a_missing_case_service_answers_503_not_401():
    with pytest.raises(HTTPException) as refused:
        check_case_service_available(None)
    assert refused.value.status_code == 503
    assert refused.value.detail == "Case service unavailable"
