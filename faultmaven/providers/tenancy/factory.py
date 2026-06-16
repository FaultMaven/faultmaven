"""Provider factory for TenantProvider (ADR-010).

Tenancy lives in the core, config-selected by ``TENANT_PROVIDER``:

- ``single`` — the built-in Standalone default (single-tenant).
- ``multi``  — the in-core multi-tenant provider (Cloud).

Both are in-core; the factory selects between them. Multi-tenancy requires the
cloud stack (PostgreSQL + RLS, OAuth/RS256, Redis); that precondition is enforced
at startup by the deployment coherence gate
(``faultmaven.config.deployment_coherence``), which crashes the process before
the container is built if ``multi`` is configured outside a cloud deployment. An
unrecognized provider name fails **closed** rather than silently downgrading to
single-tenant (which would blend access modes).
"""

import logging
from typing import Optional

from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces_user import (
    IEnterpriseRepository,
    IOrganizationRepository,
)
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)

#: The single-tenant provider built into the core (Standalone default).
BUILTIN_SINGLE = "single"
#: The multi-tenant provider built into the core (Cloud).
BUILTIN_MULTI = "multi"


class TenancyConfigurationError(RuntimeError):
    """Fatal: an unrecognized ``TENANT_PROVIDER`` was configured."""


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


def create_tenant_provider(
    organization_repository: IOrganizationRepository,
    enterprise_repository: Optional[IEnterpriseRepository] = None,
) -> TenantProvider:
    """Build the configured tenant provider (``single`` or ``multi``).

    Args:
        organization_repository: Organization repository for persistence.
        enterprise_repository: Enterprise repository, used by the single-tenant
            default for its default-enterprise bootstrap. The multi-tenant
            provider does not use it.

    Returns:
        The built ``TenantProvider``.

    Raises:
        TenancyConfigurationError: An unrecognized provider is configured
            (fail closed — never downgrades to single-tenant).
    """
    requested = requested_tenant_provider()

    if requested == BUILTIN_SINGLE:
        logger.info("Tenant provider: built-in 'single' (single-tenant)")
        return SingleTenantProvider(
            organization_repository=organization_repository,
            enterprise_repository=enterprise_repository,
        )

    if requested == BUILTIN_MULTI:
        logger.info("Tenant provider: built-in 'multi' (multi-tenant)")
        return MultiTenantProvider(organization_repository=organization_repository)

    msg = (
        f"TENANT_PROVIDER='{requested}' is not a recognized provider "
        f"(expected '{BUILTIN_SINGLE}' or '{BUILTIN_MULTI}'). "
        "Refusing to start rather than silently downgrading to single-tenant."
    )
    logger.critical(msg)
    raise TenancyConfigurationError(msg)
