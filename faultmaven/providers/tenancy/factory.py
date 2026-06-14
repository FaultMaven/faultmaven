"""Provider factory for TenantProvider (ADR-006 entry-point seam).

``single`` is FaultMaven Community Edition's built-in default. Any other
provider (e.g. ``multi``) is a paid/cloud capability supplied by an installed
plugin that registers itself under the ``faultmaven.providers.tenancy``
entry-point group — the open-source core never imports the cloud package by
name. A plugin's entry point must resolve to a *builder callable*
``build(organization_repository, enterprise_repository=None) -> TenantProvider``
(not the provider class itself). If a non-``single`` provider is configured but
its plugin is not installed, the factory fails **closed** rather than silently
downgrading to single-tenant (which would blend access modes).

The authoritative fail-closed guard runs earlier, at startup, in the deployment
coherence gate (``faultmaven.config.deployment_coherence``), which crashes the
process before the container is built. The check here is the backstop and keeps
the factory unit-testable in isolation.
"""

import logging
from importlib.metadata import EntryPoint, entry_points
from typing import Optional

from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces_user import (
    IEnterpriseRepository,
    IOrganizationRepository,
)
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)

#: Entry-point group third-party tenancy providers register under.
TENANCY_ENTRY_POINT_GROUP = "faultmaven.providers.tenancy"
#: The only provider built into the open-source core.
BUILTIN_SINGLE = "single"


class TenancyConfigurationError(RuntimeError):
    """Fatal: the configured non-``single`` tenant provider is not installed."""


def coerce_provider_name(tp: object) -> str:
    """Normalize a tenant-provider value (enum / str / None) to a lower-case name.

    ``None`` (unset) maps to the built-in ``single`` default. Shared by the
    factory and the startup coherence gate so they cannot diverge on the naming.
    """
    if tp is None:
        return BUILTIN_SINGLE
    return str(getattr(tp, "value", tp)).lower()


def requested_tenant_provider() -> str:
    """Return the configured tenant-provider name (lower-cased)."""
    return coerce_provider_name(get_settings().providers.tenant_provider)


def find_tenant_provider_plugin(name: str) -> Optional[EntryPoint]:
    """Return the installed entry point named ``name`` under the tenancy group.

    Returns ``None`` only when the plugin is genuinely absent. A discovery
    failure (e.g. corrupt/duplicate distribution metadata) is fatal and raises
    ``TenancyConfigurationError`` with the real cause, rather than masquerading
    as "plugin not installed". ``single`` is built in and is never a plugin.
    """
    try:
        eps = entry_points(group=TENANCY_ENTRY_POINT_GROUP)
    except Exception as exc:
        raise TenancyConfigurationError(
            f"Failed to discover tenancy plugins under "
            f"'{TENANCY_ENTRY_POINT_GROUP}': {exc}"
        ) from exc
    return next((ep for ep in eps if ep.name == name), None)


def create_tenant_provider(
    organization_repository: IOrganizationRepository,
    enterprise_repository: Optional[IEnterpriseRepository] = None,
) -> TenantProvider:
    """Build the configured tenant provider.

    Args:
        organization_repository: Organization repository for persistence.
        enterprise_repository: Enterprise repository (single-tenant default
            bootstrap). Forwarded to plugins, which may ignore it.

    Returns:
        The built ``TenantProvider``.

    Raises:
        TenancyConfigurationError: A non-``single`` provider is configured but no
            matching plugin is installed (fail closed — never downgrades).
    """
    requested = requested_tenant_provider()

    if requested == BUILTIN_SINGLE:
        logger.info("Tenant provider: built-in 'single' (single-tenant)")
        return SingleTenantProvider(
            organization_repository=organization_repository,
            enterprise_repository=enterprise_repository,
        )

    ep = find_tenant_provider_plugin(requested)
    if ep is None:
        msg = (
            f"TENANT_PROVIDER='{requested}' requires a tenancy plugin registered "
            f"under '{TENANCY_ENTRY_POINT_GROUP}', but none is installed. "
            "Multi-tenancy is a cloud capability — install faultmaven-cloud. "
            "Refusing to fall back to single-tenant."
        )
        logger.critical(msg)
        raise TenancyConfigurationError(msg)

    builder = ep.load()
    try:
        provider = builder(
            organization_repository=organization_repository,
            enterprise_repository=enterprise_repository,
        )
    except TypeError as exc:
        raise TenancyConfigurationError(
            f"Tenancy plugin '{requested}' ({ep.value}) is not a valid builder: it "
            "must be callable as "
            "build(organization_repository, enterprise_repository=None) -> "
            f"TenantProvider, not the provider class itself ({exc})."
        ) from exc
    if not isinstance(provider, TenantProvider):
        raise TenancyConfigurationError(
            f"Tenancy plugin '{requested}' ({ep.value}) returned "
            f"{type(provider).__name__}, not a TenantProvider."
        )
    logger.info("Tenant provider: '%s' loaded from plugin %s", requested, ep.value)
    return provider
