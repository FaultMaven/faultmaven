"""Provider factory for TenantProvider (ADR-010).

Tenancy lives in the core, config-selected by ``TENANT_PROVIDER``:

- ``single`` — the built-in Standalone default (single-tenant).
- ``multi``  — the in-core multi-tenant provider (Cloud). Requires
  ``DEPLOYMENT_MODE=cloud``: its isolation is PostgreSQL row-level security
  scoped by the per-request **enterprise** binding, and the cloud coherence
  checks (PostgreSQL, OAuth/RS256, Redis) are what guarantee that stack. The
  factory fails **closed** on ``multi`` outside cloud — here, so the gate-less
  jobs/CLI path is covered too, not only the startup coherence gate.

An unrecognized provider name also fails **closed** rather than silently
downgrading to single-tenant (which would blend access modes).
"""

import logging
from typing import Optional

from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces_user import (
    IEnterpriseRepository,
    ITeamRepository,
)
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)

#: The single-tenant provider built into the core (Standalone default).
BUILTIN_SINGLE = "single"
#: The multi-tenant provider built into the core (Cloud).
BUILTIN_MULTI = "multi"

#: Shared by the factory and the coherence gate so the message cannot diverge.
MULTI_REQUIRES_CLOUD_MSG = (
    "TENANT_PROVIDER='multi' requires DEPLOYMENT_MODE=cloud: multi-tenant "
    "isolation is PostgreSQL row-level security scoped by the per-request "
    "enterprise binding, and the cloud stack (PostgreSQL, OAuth/RS256, Redis) "
    "is what provides it. Set DEPLOYMENT_MODE=cloud, or use "
    "TENANT_PROVIDER=single."
)


class TenancyConfigurationError(RuntimeError):
    """Fatal: an unrecognized or not-yet-available ``TENANT_PROVIDER``."""


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
    enterprise_repository: Optional[IEnterpriseRepository] = None,
    team_repository: Optional[ITeamRepository] = None,
) -> TenantProvider:
    """Build the configured tenant provider (``single`` or ``multi``).

    Args:
        enterprise_repository: Enterprise repository. The single-tenant default
            uses it for its default-enterprise bootstrap; the multi-tenant
            provider resolves the request's tenant through it.
        team_repository: Team repository, used by the single-tenant default to
            seed the default team row. The multi-tenant provider does not use it.

    Returns:
        The built ``TenantProvider``.

    Raises:
        TenancyConfigurationError: ``multi`` is configured outside
            ``DEPLOYMENT_MODE=cloud``, or an unrecognized provider is configured
            (fail closed — never downgrades to single-tenant).
    """
    requested = requested_tenant_provider()

    if requested == BUILTIN_SINGLE:
        logger.info("Tenant provider: built-in 'single' (single-tenant)")
        return SingleTenantProvider(
            enterprise_repository=enterprise_repository,
            team_repository=team_repository,
        )

    if requested == BUILTIN_MULTI:
        if not get_settings().is_cloud:
            logger.critical(MULTI_REQUIRES_CLOUD_MSG)
            raise TenancyConfigurationError(MULTI_REQUIRES_CLOUD_MSG)
        logger.info("Tenant provider: built-in 'multi' (multi-tenant)")
        return MultiTenantProvider(enterprise_repository=enterprise_repository)

    msg = (
        f"TENANT_PROVIDER='{requested}' is not a recognized provider "
        f"(expected '{BUILTIN_SINGLE}' or '{BUILTIN_MULTI}'). "
        "Refusing to start rather than silently downgrading to single-tenant."
    )
    logger.critical(msg)
    raise TenancyConfigurationError(msg)
