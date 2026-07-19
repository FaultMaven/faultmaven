"""Provider factory for TenantProvider (ADR-010).

Tenancy lives in the core, config-selected by ``TENANT_PROVIDER``:

- ``single`` — the built-in Standalone default (single-tenant).
- ``multi``  — the in-core multi-tenant provider (Cloud). Config-selectable and
  the provider exists, but NOT yet bootable: it is held behind
  ``MULTI_TENANT_READY`` until the row-level isolation it requires (PostgreSQL
  RLS) and the request->organization wiring ship (ADR-010 P2). Until then the
  factory fails **closed** on ``multi`` — here, so the gate-less jobs/CLI path is
  covered too, not only the startup coherence gate.

An unrecognized provider name also fails **closed** rather than silently
downgrading to single-tenant (which would blend access modes).
"""

import logging
from typing import Optional

from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces_user import (
    IEnterpriseRepository,
    IOrganizationRepository,
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

#: ``multi`` is config-selectable and the in-core provider exists, but it is NOT
#: yet bootable: the row-level isolation it requires (PostgreSQL RLS) and the
#: request->organization wiring have not shipped, so a multi-tenant deployment
#: would mis-scope writes and serve unscoped reads. Until then the factory and
#: the coherence gate both fail closed on ``multi``. Flip to ``True`` in the
#: phase that lands the RLS migration + tenant-context wiring (ADR-010 P2).
MULTI_TENANT_READY = False

#: Shared by the factory and the coherence gate so the message cannot diverge.
MULTI_NOT_READY_MSG = (
    "TENANT_PROVIDER='multi' is not yet available: multi-tenant row-level "
    "isolation (PostgreSQL RLS) and request->organization wiring have not "
    "shipped, so a multi-tenant deployment would mis-scope writes and serve "
    "unscoped reads. Use TENANT_PROVIDER=single. "
    "(Tracked: ADR-010 forward-consolidation P2.)"
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
    organization_repository: IOrganizationRepository,
    enterprise_repository: Optional[IEnterpriseRepository] = None,
    team_repository: Optional[ITeamRepository] = None,
) -> TenantProvider:
    """Build the configured tenant provider (``single`` or ``multi``).

    Args:
        organization_repository: Organization repository for persistence.
        enterprise_repository: Enterprise repository, used by the single-tenant
            default for its default-enterprise bootstrap. The multi-tenant
            provider does not use it.
        team_repository: Team repository, used by the single-tenant default to
            seed the default team row. The multi-tenant provider does not use it.

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
            team_repository=team_repository,
        )

    if requested == BUILTIN_MULTI:
        if not MULTI_TENANT_READY:
            logger.critical(MULTI_NOT_READY_MSG)
            raise TenancyConfigurationError(MULTI_NOT_READY_MSG)
        logger.info("Tenant provider: built-in 'multi' (multi-tenant)")
        return MultiTenantProvider(organization_repository=organization_repository)

    msg = (
        f"TENANT_PROVIDER='{requested}' is not a recognized provider "
        f"(expected '{BUILTIN_SINGLE}' or '{BUILTIN_MULTI}'). "
        "Refusing to start rather than silently downgrading to single-tenant."
    )
    logger.critical(msg)
    raise TenancyConfigurationError(msg)
