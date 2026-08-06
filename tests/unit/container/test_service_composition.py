"""``register_services`` is called here, not patched (#959).

Every other test of container composition patches ``register_services`` away
(``test_tenancy_fail_closed``, ``test_degraded_init_visibility``,
``test_cloud_init_fail_fast``), and the tests of the individual factories call
those factories directly. Between the two sits the code nothing executed: the
call sites. A signature change to ``create_jwt_token_generator`` left its only
production caller passing the old keyword arguments, which is a ``TypeError``
raised inside ``register_services`` on the ``oauth_enabled`` branch — before the
tenant provider, case service, investigation engine, session service and the
rest are registered — i.e. a CrashLoop on every OAuth deployment. Both halves
were green: the factory test called the factory correctly, and the composition
tests never ran the caller.

So this file runs the real function against a real container and asserts that
composition COMPLETES and that services registered *after* the generator are
present. Stubs are placed only at the true boundaries — the infrastructure
services that would otherwise open a database connection, a Redis socket or an
HTTP client. Nothing between ``register_services`` and the factories it calls is
patched, because that is precisely the code under test.

Note for anyone probing this by hand: ``python /abs/path/probe.py`` puts the
SCRIPT's directory on ``sys.path[0]``, not the cwd. A probe script living
outside the worktree therefore imports the shared checkout however carefully
the cwd and ``PYTHONPATH`` were set — and reports on code you did not change.
Put probe scripts inside the tree they are probing.
"""

from __future__ import annotations

import logging

import pytest

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container import DIContainer
from faultmaven.container.providers.services import register_services
from faultmaven.container.registry import DependencyRegistry

#: Infrastructure the service layer reads out of the container. These are the
#: real boundary: each one owns a socket or a model, and ``register_services``
#: only passes them along. Four of them are fetched with ``required=True``, so
#: they cannot simply be absent.
BOUNDARY_SERVICES = (
    "session_store",
    "vector_store",
    "llm_provider",
    "data_classifier",
    "log_processor",
    "sanitizer",
    "tracer",
    "preprocessing_service",
    "file_storage_service",
)


def _settings(monkeypatch, **env) -> FaultMavenSettings:
    """A real settings object — the composition reads too much of it to fake."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "standalone")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("TENANT_PROVIDER", "single")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return FaultMavenSettings(_env_file=None)


def _container(settings: FaultMavenSettings) -> DIContainer:
    """A real container, built off the singleton.

    ``DIContainer.__new__`` returns the process-wide instance, so calling it
    here would compose into whatever state another test left behind — and leave
    this test's services there for the next one. ``object.__new__`` gives the
    real class with a private registry.
    """
    container = object.__new__(DIContainer)
    container._initialized = False
    container._initializing = True
    container._registry = DependencyRegistry()
    container._logger = logging.getLogger("test.composition")
    container.settings = settings

    for name in BOUNDARY_SERVICES:
        container._register_service(name, object())

    # Attributes the service layer reads directly. None is the honest value:
    # no Redis client and no case repository in this process.
    container.redis_client = None
    container.case_repository = None
    container.user_store = object()
    return container


@pytest.mark.unit
class TestOAuthDeploymentComposes:
    """The quadrant the broken call site took down."""

    def test_composition_completes(self, monkeypatch):
        settings = _settings(monkeypatch, AUTH_MODE="oauth", OAUTH_ENABLED="true")
        assert settings.auth.oauth_enabled is True

        container = _container(settings)

        # No pytest.raises: the assertion IS that this returns.
        register_services(container)

        assert container.get_service("jwt_token_generator") is not None

    def test_services_after_the_generator_are_registered(self, monkeypatch):
        """A TypeError at the generator leaves all of these missing.

        Named individually rather than counted, because the failure mode is
        "composition stopped here" and the useful signal is how far it got.
        """
        settings = _settings(monkeypatch, AUTH_MODE="oauth", OAUTH_ENABLED="true")
        container = _container(settings)

        register_services(container)

        for name in (
            "oauth_service",
            "tenant_provider",
            "case_service",
            "session_service",
            "data_service",
            "agent_service",
        ):
            assert container.get_service(name) is not None, name

    def test_both_generators_carry_the_auth_services_key(self, monkeypatch):
        """#959's property, asserted through the composition that wires it.

        Nothing is declared in this environment, so ``AuthService`` selects a
        development pair — the case where a second resolver would produce a
        second pair and every minted token would fail verification.
        """
        settings = _settings(monkeypatch, AUTH_MODE="oauth", OAUTH_ENABLED="true")
        container = _container(settings)

        register_services(container)

        auth_service = container.get_service("auth_service")
        assert auth_service.signing_private_key

        for name in ("signing_token_generator", "jwt_token_generator"):
            generator = container.get_service(name)
            assert generator.private_key == auth_service.signing_private_key, name
            assert generator.public_key == auth_service.verification_public_key, name


@pytest.mark.unit
class TestLocalDeploymentComposes:
    """The other branch of the same ``if``, which has its own wiring."""

    def test_composition_completes_and_signs_hs256(self, monkeypatch):
        settings = _settings(monkeypatch, AUTH_MODE="local", JWT_SECRET_KEY="x" * 40)
        container = _container(settings)

        register_services(container)

        from faultmaven.modules.auth.domain.services.jwt_token_generator import (
            HS256JWTTokenGenerator,
        )

        generator = container.get_service("signing_token_generator")
        assert isinstance(generator, HS256JWTTokenGenerator)
        assert container.get_service("user_service") is not None
        # OAuth is off: its services are absent, and everything downstream of
        # them is still registered.
        assert container.get_service("case_service") is not None


@pytest.mark.unit
class TestCompositionWithNoSigningKey:
    """Local mode where the JWT secret never resolved.

    Reachable in production, not hypothetical: ``ensure_local_jwt_secret_env``
    warns and returns when it cannot write ``data/.jwt_secret``, leaving
    ``JWT_SECRET_KEY`` unset. Everything that does not sign must survive it —
    the admin user routes read ``app.state.user_service`` and raise a bare
    RuntimeError when it is absent, so an unwritable directory would otherwise
    turn every ``/api/v1/admin/users`` request into a 500.
    """

    def _composed(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        settings = _settings(monkeypatch, AUTH_MODE="local")
        assert settings.security.jwt_secret_key is None, "the no-key state"

        container = _container(settings)
        register_services(container)
        return container

    def test_composition_completes_and_names_the_absent_generator(self, monkeypatch):
        container = self._composed(monkeypatch)

        # Assigned, not merely unregistered: a direct attribute read — the
        # pattern every other container service uses — must see None rather
        # than raise AttributeError.
        assert container.signing_token_generator is None
        assert container.get_service("signing_token_generator") is None

    def test_user_management_survives_it(self, monkeypatch):
        container = self._composed(monkeypatch)

        user_service = container.get_service("user_service")
        assert user_service is not None, (
            "admin user routes raise RuntimeError without this service; a "
            "missing signing key must not take user management down"
        )
        assert user_service.token_generator is None

    def test_downstream_services_are_still_registered(self, monkeypatch):
        container = self._composed(monkeypatch)

        for name in ("tenant_provider", "case_service", "session_service"):
            assert container.get_service(name) is not None, name

    @pytest.mark.asyncio
    async def test_the_reset_flow_refuses_uniformly(self, monkeypatch):
        """Every input class gets the same refusal, and no dead token.

        Uniform by construction: the refusal precedes the account lookup, so it
        cannot depend on whether the address exists.
        """
        import fakeredis.aioredis as fakeredis_aio

        from faultmaven.exceptions import ServiceError
        from faultmaven.infrastructure.persistence.user_repository import (
            InMemoryUserRepository,
        )

        container = self._composed(monkeypatch)
        user_service = container.get_service("user_service")
        user_service.user_repo = InMemoryUserRepository()
        user_service.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)

        live = await user_service.register_user(
            email="live@local.faultmaven",
            password="Str0ng-P4ssw0rd!",
            full_name="Live",
        )
        deactivated = await user_service.register_user(
            email="gone@local.faultmaven",
            password="Str0ng-P4ssw0rd!",
            full_name="Gone",
        )
        stored = await user_service.user_repo.get(deactivated.user_id)
        stored.is_active = False
        await user_service.user_repo.save(stored)
        assert live.user_id != deactivated.user_id

        observables = set()
        for email in (
            "live@local.faultmaven",
            "nobody@local.faultmaven",
            "gone@local.faultmaven",
        ):
            with pytest.raises(ServiceError) as refusal:
                await user_service.request_password_reset(email=email)
            observables.add((type(refusal.value), str(refusal.value)))

        assert len(observables) == 1, observables
        assert await user_service.redis_client.keys("password_reset:*") == []
