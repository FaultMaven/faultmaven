"""Provider Factory for TenantProvider.

Creates appropriate TenantProvider implementation based on provider selector
configuration from unified settings.
"""

import logging

from faultmaven.config.settings import TenantProvider as TenantProviderEnum
from faultmaven.config.settings import get_settings
from faultmaven.models.interfaces_user import IOrganizationRepository
from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)


def create_tenant_provider(
    organization_repository: IOrganizationRepository,
) -> TenantProvider:
    """Factory function to create appropriate TenantProvider based on settings.

    Selects provider implementation based on TENANT_PROVIDER environment variable:
    - "single": SingleTenantProvider (local, community, development)
    - "multi": MultiTenantProvider (cloud, enterprise, production)

    Args:
        organization_repository: Organization repository for persistence

    Returns:
        TenantProvider: SingleTenantProvider or MultiTenantProvider instance

    Environment Variables:
        TENANT_PROVIDER: "single" | "multi" (default: "single")

    Design Notes:
        - Defaults to single-tenant for local development ease
        - Multi-tenant requires explicit configuration for production safety
        - Provider instance is singleton-scoped via DI container
    """
    settings = get_settings()
    tenant_provider = settings.providers.tenant_provider

    if tenant_provider == TenantProviderEnum.MULTI:
        logger.info(
            "Creating MultiTenantProvider (cloud/enterprise mode) [TENANT_PROVIDER=multi]"
        )
        return MultiTenantProvider(organization_repository=organization_repository)
    else:
        # Default to single-tenant (local, community, development)
        logger.info(
            "Creating SingleTenantProvider (local/community mode) [TENANT_PROVIDER=single]"
        )
        return SingleTenantProvider(organization_repository=organization_repository)
