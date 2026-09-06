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
"""

from unittest.mock import MagicMock, patch

import pytest

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
