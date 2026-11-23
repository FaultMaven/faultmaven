"""Tenancy misconfiguration must terminate container initialization (ADR-010 P2e).

A ``TenancyConfigurationError`` is a deliberate fail-closed refusal (e.g.
``TENANT_PROVIDER=multi`` outside ``DEPLOYMENT_MODE=cloud``), not an
infrastructure hiccup. ``DIContainer.initialize()`` must re-raise it on EVERY
path — jobs/CLI included — never swallow it into a half-initialized container
that would run against tenanted data unchecked. And the service wiring must not
silently skip the factory (and with it the fail-closed checks) when the
ENTERPRISE repository is unavailable under ``multi`` — the enterprise is what
the provider resolves through (ADR-017 D1), so its absence is the one that
leaves a multi-tenant deployment with no tenant provider at all.

Under ``multi``, a construction failure of any factory that feeds tenancy or
sharing refuses the start too (#2342): ``None`` there reads as "not configured
for it", an answer a swallowed exception may not give.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.container.errors import RequiredServiceConstructionError
from faultmaven.container.providers import services
from faultmaven.container.providers.services import create_tenant_provider
from faultmaven.providers.tenancy.factory import TenancyConfigurationError


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.asyncio
async def test_container_initialize_reraises_tenancy_configuration_error():
    """The blanket exception handling in initialize() must not swallow a
    tenancy refusal into ``_initialized = False`` + normal return."""
    from faultmaven._container_impl import DIContainer

    container = DIContainer()
    container._initialized = False
    container._initializing = False

    with patch(
        "faultmaven._container_impl.register_infrastructure", new=MagicMock()
    ) as reg_infra:
        reg_infra.side_effect = None
        reg_infra.return_value = _awaitable_none()
        with patch("faultmaven._container_impl.register_tools"):
            with patch(
                "faultmaven._container_impl.register_services",
                side_effect=TenancyConfigurationError("multi requires cloud"),
            ):
                with pytest.raises(TenancyConfigurationError):
                    await container.initialize()

    assert container._initialized is False


def _awaitable_none():
    async def _noop(*_args, **_kwargs):
        return None

    return _noop()


@pytest.mark.unit
@pytest.mark.security
def test_missing_enterprise_repository_is_fatal_under_multi():
    """Under ``multi``, a missing enterprise repository must not silently skip
    the tenancy factory (and its multi-requires-cloud check)."""
    with patch(
        "faultmaven.providers.tenancy.factory.get_settings",
        return_value=_settings("multi"),
    ):
        with pytest.raises(TenancyConfigurationError):
            create_tenant_provider(
                settings=_settings("multi"), enterprise_repository=None
            )


@pytest.mark.unit
def test_missing_enterprise_repository_still_skips_under_single():
    """Single-tenant keeps the pre-existing graceful skip."""
    with patch(
        "faultmaven.providers.tenancy.factory.get_settings",
        return_value=_settings("single"),
    ):
        assert (
            create_tenant_provider(
                settings=_settings("single"), enterprise_repository=None
            )
            is None
        )


def _settings(tenant_provider: str) -> MagicMock:
    s = MagicMock()
    s.providers = MagicMock()
    s.providers.tenant_provider = tenant_provider
    s.is_cloud = False
    return s


_PERSISTENCE = "faultmaven.infrastructure.persistence."

#: (factory, the constructor it calls, the name its refusal gives).
_FACTORIES = pytest.mark.parametrize(
    "build,target,name",
    [
        (
            services.create_organization_repository,
            _PERSISTENCE
            + "sessionless_organization_repository.SessionlessOrganizationRepository",
            "OrganizationRepository",
        ),
        (
            services.create_enterprise_repository,
            _PERSISTENCE
            + "sessionless_enterprise_repository.SessionlessEnterpriseRepository",
            "EnterpriseRepository",
        ),
        (
            services.create_team_repository,
            _PERSISTENCE + "sessionless_team_repository.SessionlessTeamRepository",
            "TeamRepository",
        ),
        (
            services.create_share_repository,
            _PERSISTENCE + "sessionless_share_repository.SessionlessShareRepository",
            "ShareRepository",
        ),
        (
            lambda: create_tenant_provider(
                settings=MagicMock(), enterprise_repository=MagicMock()
            ),
            "faultmaven.providers.tenancy.factory.create_tenant_provider",
            "TenantProvider",
        ),
    ],
    ids=["organization", "enterprise", "team", "share", "tenant_provider"],
)


@pytest.mark.unit
@pytest.mark.security
@_FACTORIES
def test_a_factory_that_fails_to_build_refuses_under_multi(build, target, name, caplog):
    """The refusal names the class, never the text, and chains the original.
    The factory's warning is logged first: the jobs path reports the refusal
    by class only, never its message or the factory's name, so the warning is
    what names the factory there."""
    error = ImportError("marker")
    with (
        patch(
            "faultmaven.providers.tenancy.factory.get_settings",
            return_value=_settings("multi"),
        ),
        patch(target, side_effect=error),
    ):
        with pytest.raises(RequiredServiceConstructionError) as refused:
            build()

    message = str(refused.value)
    assert message.startswith(f"{name} could not be constructed")
    assert "ImportError" in message
    assert "marker" not in message
    assert refused.value.__cause__ is error
    assert f"{name} initialization failed: ImportError" in caplog.text


@pytest.mark.unit
@_FACTORIES
def test_a_factory_that_fails_to_build_is_none_under_single(
    build, target, name, caplog
):
    """The control: single-tenant warns and returns ``None``, as before."""
    with (
        patch(
            "faultmaven.providers.tenancy.factory.get_settings",
            return_value=_settings("single"),
        ),
        patch(target, side_effect=ImportError("marker")),
    ):
        assert build() is None

    assert f"{name} initialization failed: ImportError" in caplog.text


@pytest.mark.unit
@pytest.mark.security
@pytest.mark.parametrize(
    "provider,repository,missing",
    [
        (None, MagicMock(), "no tenant provider"),
        (MagicMock(), None, "no team repository"),
    ],
    ids=["no_provider", "no_team_repository"],
)
def test_the_team_service_refuses_a_missing_dependency_under_multi(
    provider, repository, missing
):
    """Defence in depth: ``register_services`` cannot reach this after the
    factories refuse, but ``None`` here would still mean "no team sharing"."""
    with patch(
        "faultmaven.providers.tenancy.factory.get_settings",
        return_value=_settings("multi"),
    ):
        with pytest.raises(RequiredServiceConstructionError, match=missing):
            services.create_team_service(provider, repository, MagicMock())


@pytest.mark.unit
@pytest.mark.security
def test_the_team_service_refuses_when_it_cannot_be_built_under_multi():
    """Past the single-tenant check, a failure to build the service itself
    refuses the start by class, never by text, like its dependencies."""
    error = ImportError("marker")
    with patch(
        "faultmaven.modules.auth.domain.services.team_service.TeamService",
        side_effect=error,
    ):
        with pytest.raises(RequiredServiceConstructionError) as refused:
            services.create_team_service(MagicMock(), MagicMock(), MagicMock())

    message = str(refused.value)
    assert message.startswith("TeamService could not be constructed")
    assert "ImportError" in message
    assert "marker" not in message
    assert refused.value.__cause__ is error
