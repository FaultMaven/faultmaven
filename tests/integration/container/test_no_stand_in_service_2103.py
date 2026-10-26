"""A configured service that cannot be built refuses startup everywhere (#2103).

Drives the real composition. The case repository and the case, session and
auth services were replaced by a stand-in when their construction failed, and
a development deployment that composed without them served anyway. Now
``DIContainer.initialize`` re-raises ``RequiredServiceConstructionError`` ahead
of its lenient path, so the process does not start in any environment, and
the refusal names the cause.
"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.modules.auth.domain.services.auth_service import (
    PartialKeyConfigurationError,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONSTRUCTORS = pytest.mark.parametrize(
    "target,name",
    [
        (
            "faultmaven.modules.case.infrastructure.sessionless_case_repository."
            "SessionlessCaseRepository",
            "Case repository",
        ),
        (
            "faultmaven.modules.case.domain.services.case_service.CaseService",
            "CaseService",
        ),
        (
            "faultmaven.modules.auth.domain.services.auth_session_service."
            "AuthSessionService",
            "AuthSessionService",
        ),
        (
            "faultmaven.modules.auth.domain.services.auth_service.AuthService",
            "AuthService",
        ),
    ],
    ids=["case_repository", "case", "session", "auth"],
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
    error = RuntimeError("boom")

    def _break(target: str):
        def _raise(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(f"{target}.__init__", _raise)
        return error

    return _break


@CONSTRUCTORS
@pytest.mark.parametrize("env", ["development", "production"])
async def test_the_boot_refuses_and_names_the_error(
    target, name, env, boom, environment, fresh_container, monkeypatch
):
    settings = environment(env)
    error = boom(target)

    async def _went_on(*_args, **_kwargs):
        pytest.fail(f"composition went on past a {name} it could not build")

    monkeypatch.setattr("faultmaven.bootstrap.startup.bootstrap_application", _went_on)
    from faultmaven.bootstrap.composition import compose_application

    with pytest.raises(RequiredServiceConstructionError) as refused:
        await compose_application(FastAPI(), settings)
    assert refused.value.__cause__ is error
    assert str(refused.value) == f"{name} could not be constructed: boom"


@CONSTRUCTORS
async def test_a_lenient_container_refuses_too(
    target, name, boom, environment, fresh_container
):
    """``allow_degraded`` asks for the partial container; it gets the refusal."""
    assert not environment("development").must_not_degrade
    error = boom(target)

    with pytest.raises(RequiredServiceConstructionError) as refused:
        await fresh_container.initialize(allow_degraded=True)

    assert refused.value.__cause__ is error
    assert not fresh_container.is_initialized


async def test_half_a_key_pair_refuses_a_development_boot(
    environment, fresh_container, monkeypatch
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    monkeypatch.setenv("JWT_PUBLIC_KEY", public_pem.decode())
    assert not environment("development").must_not_degrade

    with pytest.raises(RequiredServiceConstructionError) as refused:
        await fresh_container.initialize(allow_degraded=True)

    assert isinstance(refused.value.__cause__, PartialKeyConfigurationError)
