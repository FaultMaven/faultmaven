"""Application Startup Bootstrapper (TASK-023).

Handles critical startup tasks that must run before the application
can accept requests. This includes creating the default organization
for single-tenant deployments.

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

import logging
from typing import Any

from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

logger = logging.getLogger(__name__)


async def bootstrap_application(container: Any) -> None:
    """Application startup bootstrapper.

    Runs critical initialization tasks during application startup.

    Tasks:
    1. Create default organization (single-tenant mode only)
    2. Verify database schema (future)
    3. Initialize infrastructure providers (future)

    Args:
        container: DI container with initialized providers

    Raises:
        Exception: If critical bootstrap tasks fail

    Design Notes:
        - Single-tenant: Ensures default organization exists
        - Multi-tenant: No default organization created
        - Idempotent: Safe to call multiple times
        - Blocks startup if critical tasks fail

    Integration:
        This function is called from main.py during FastAPI startup:

        @app.on_event("startup")
        async def startup_event():
            await bootstrap_application(container)
    """
    logger.info("Starting application bootstrap")

    # Get tenant provider from container
    if not hasattr(container, 'tenant_provider') or container.tenant_provider is None:
        logger.warning("TenantProvider not available in container - skipping bootstrap")
        return

    tenant_provider = container.tenant_provider

    # Single-tenant mode: Ensure default organization exists
    if isinstance(tenant_provider, SingleTenantProvider):
        logger.info("Single-tenant mode: Ensuring default organization exists")
        try:
            default_org = await tenant_provider.ensure_default_organization_exists()
            logger.info(
                f"Default organization ready: {default_org.name} "
                f"(ID: {default_org.org_id}, Tier: {default_org.plan_tier.value})"
            )
        except Exception as e:
            logger.error(f"Failed to create default organization: {e}")
            raise
    else:
        logger.info("Multi-tenant mode: No default organization created")

    # Future: Add more bootstrap tasks here
    # - Verify database schema
    # - Initialize infrastructure providers
    # - Load system configuration

    logger.info("Application bootstrap complete")
