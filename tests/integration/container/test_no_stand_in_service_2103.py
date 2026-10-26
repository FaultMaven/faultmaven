"""A configured service that cannot be built never serves as a stand-in (#2103).

Drives the real composition. Where the deployment must not degrade, the boot
refuses and names the constructor's error. Where it may serve partially, the
service is absent: in neither case is an in-memory case or session service,
or an ``AuthService`` without a revocation store, put in its place.
"""

import pytest
from fastapi import FastAPI

from faultmaven.modules.auth.domain.services.auth_service import AuthService
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.case.domain.services.case_service import CaseService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SERVICES = pytest.mark.parametrize(
    "cls,name",
    [
        (CaseService, "case_service"),
        (AuthSessionService, "session_service"),
        (AuthService, "auth_service"),
    ],
    ids=["case", "session", "auth"],
)


@pytest.fixture
def environment(monkeypatch):
    """Compose under the given ``ENVIRONMENT``; the settings singleton is rebuilt
    from it and again afterwards."""
    from faultmaven.config.settings import get_settings, reset_settings

    def _set(value: str):
        monkeypatch.setenv("ENVIRONMENT", value)
        reset_settings()
        return get_settings()

    yield _set
    monkeypatch.undo()
    reset_settings()


@pytest.fixture
def boom(monkeypatch):
    """Make the configured call of ``cls`` raise. A bare construction, the
    ``AuthService()`` the auth factory used to fall back to, still builds."""
    error = RuntimeError("boom")

    def _break(cls):
        original = cls.__init__

        def _refuse_the_configured_call(self, *args, **kwargs):
            if args or kwargs:
                raise error
            original(self)

        monkeypatch.setattr(cls, "__init__", _refuse_the_configured_call)
        return error

    return _break


@SERVICES
async def test_the_boot_refuses_and_names_the_error(
    cls, name, boom, environment, fresh_container, monkeypatch
):
    settings = environment("production")
    assert settings.must_not_degrade
    error = boom(cls)

    async def _went_on(*_args, **_kwargs):
        pytest.fail(f"composition went on past the {cls.__name__} it could not build")

    monkeypatch.setattr("faultmaven.bootstrap.startup.bootstrap_application", _went_on)
    from faultmaven.bootstrap.composition import compose_application

    with pytest.raises(RuntimeError) as refused:
        await compose_application(FastAPI(), settings)
    assert refused.value.__cause__ is error
    assert "ENVIRONMENT=production: boom" in str(refused.value)


@SERVICES
async def test_a_partial_deployment_serves_without_it(
    cls, name, boom, environment, fresh_container
):
    settings = environment("development")
    assert not settings.must_not_degrade
    boom(cls)

    await fresh_container.initialize(allow_degraded=True)

    assert not fresh_container.is_initialized
    assert fresh_container.get_service(name) is None
