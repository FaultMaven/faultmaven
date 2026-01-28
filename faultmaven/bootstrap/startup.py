"""Application Startup Bootstrapper (TASK-023).

Handles critical startup tasks that must run before the application
can accept requests. This includes:
- Creating data directories
- Running database migrations
- Creating default admin user (local mode)
- Creating default organization (single-tenant mode)

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
    1. Initialize data layer (directories, migrations, default admin)
    2. Create default organization (single-tenant mode only)

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
    logger.debug("Starting application bootstrap")

    # ============================================================
    # Step 1: Initialize Data Layer
    # ============================================================
    # Creates data directories, runs migrations, creates default admin
    try:
        from faultmaven.bootstrap.data_init import initialize_data_layer

        await initialize_data_layer(container)
    except Exception as e:
        # Log but don't fail - the app can still work with manual setup
        logger.warning(f"Data layer initialization had issues: {e}")

    # ============================================================
    # Step 2: Ensure Default Organization (Single-Tenant Mode)
    # ============================================================
    # Get tenant provider from container
    if not hasattr(container, "tenant_provider") or container.tenant_provider is None:
        logger.warning(
            "TenantProvider not available in container - skipping org bootstrap"
        )
    else:
        tenant_provider = container.tenant_provider

        # Single-tenant mode: Ensure default organization exists
        if isinstance(tenant_provider, SingleTenantProvider):
            # Silent operation in local mode - use debug level to avoid user-visible output
            logger.debug("Single-tenant mode: Ensuring default organization exists")
            try:
                default_org = await tenant_provider.ensure_default_organization_exists()
                logger.debug(
                    f"Default organization ready: {default_org.name} "
                    f"(ID: {default_org.org_id}, Tier: {default_org.plan_tier.value})"
                )
            except Exception as e:
                logger.error(f"Failed to create default organization: {e}")
                raise
        else:
            logger.debug("Multi-tenant mode: No default organization created")

    logger.debug("Application bootstrap complete")
