"""Provider Factory for TenantProvider.

Creates appropriate TenantProvider implementation based on deployment mode
configuration from environment variables.

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

from typing import Literal
import logging

from faultmaven.providers.tenancy.base import TenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.models.interfaces_user import IOrganizationRepository
from faultmaven.config.settings import get_settings

logger = logging.getLogger(__name__)


def create_tenant_provider(
    organization_repository: IOrganizationRepository
) -> TenantProvider:
    """Factory function to create appropriate TenantProvider based on environment.

    Selects provider implementation based on DEPLOYMENT_MODE environment variable:
    - "single-tenant": SingleTenantProvider (local, community, development)
    - "multi-tenant": MultiTenantProvider (cloud, enterprise, production)

    Args:
        organization_repository: Organization repository for persistence

    Returns:
        TenantProvider: SingleTenantProvider or MultiTenantProvider instance

    Environment Variables:
        DEPLOYMENT_MODE: "single-tenant" | "multi-tenant" (default: "single-tenant")

    Design Notes:
        - Defaults to single-tenant for local development ease
        - Multi-tenant requires explicit configuration for production safety
        - Provider instance is singleton-scoped via DI container
    """
    settings = get_settings()
    deployment_mode: Literal["single-tenant", "multi-tenant"] = settings.deployment_mode.lower()  # type: ignore

    if deployment_mode == "multi-tenant":
        logger.info("Creating MultiTenantProvider (cloud/enterprise mode)")
        return MultiTenantProvider(
            organization_repository=organization_repository
        )
    else:
        # Default to single-tenant (local, community, development)
        logger.info("Creating SingleTenantProvider (local/community mode)")
        return SingleTenantProvider(
            organization_repository=organization_repository
        )
