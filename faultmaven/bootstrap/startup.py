"""Application Startup Bootstrapper (TASK-023).

Handles critical startup tasks that must run before the application
can accept requests. This includes:
- Creating data directories
- Running database migrations
- Creating default admin user (local mode)
- Creating default organization (single-tenant mode)
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
    logger.info("Starting application bootstrap")

    # ============================================================
    # Step 1: Initialize Data Layer
    # ============================================================
    # Creates data directories, runs migrations, creates default admin
    from faultmaven.bootstrap.data_init import initialize_data_layer

    await initialize_data_layer(container)
    logger.info("Data layer initialization successful")

    # ============================================================
    # Step 2: Ensure Default Tenant (Single-Tenant Mode)
    # ============================================================
    # The enterprise is the tenant (ADR-017 D8). No organization is created:
    # an organization is a billing target and nobody is billed for a
    # self-hosted deployment, so ``organization_id`` on its rows stays NULL.
    tenant_provider = getattr(container, "tenant_provider", None)
    if tenant_provider is None:
        logger.warning(
            "TenantProvider not available in container - skipping tenant bootstrap"
        )
    else:
        if isinstance(tenant_provider, SingleTenantProvider):
            logger.info("Single-tenant mode: Ensuring default enterprise exists")
            try:
                default_enterprise = (
                    await tenant_provider.ensure_default_enterprise_exists()
                )
                if default_enterprise is not None:
                    logger.info(
                        f"Default enterprise ready: {default_enterprise.name} "
                        f"(ID: {default_enterprise.enterprise_id}, "
                        f"Tier: {default_enterprise.plan_tier.value})"
                    )
            except Exception as e:
                logger.error(f"Failed to create default enterprise: {e}")
                raise

            # Order: enterprise -> team. The team FK enterprise_id is NOT NULL,
            # so the default enterprise must exist first.
            logger.info("Single-tenant mode: Ensuring default team exists")
            try:
                default_team = await tenant_provider.ensure_default_team_exists()
                if default_team is not None:
                    logger.info(
                        f"Default team ready: {default_team.name} "
                        f"(ID: {default_team.team_id})"
                    )
            except Exception as e:
                logger.error(f"Failed to create default team: {e}")
                raise
        else:
            logger.info("Multi-tenant mode: No default tenant created")

    # ============================================================
    # Step 3: Ensure Default Admin User (Single-Tenant Mode)
    # ============================================================
    # Must run after tenant bootstrap so the enterprise_id FK is valid. The
    # default admin is the Standalone passwordless account inside the default
    # enterprise — under multi-tenant identities come from the IdP, so creating
    # it must not even be attempted (it would plant a passwordless admin in the
    # Standalone enterprise, which multi-tenant deployments still carry as the
    # sentinel).
    if tenant_provider is not None and not isinstance(
        tenant_provider, SingleTenantProvider
    ):
        logger.info("Multi-tenant mode: skipping default admin bootstrap")
    else:
        from faultmaven.bootstrap.data_init import ensure_default_admin_exists

        logger.info("Ensuring default admin user exists")
        admin_user = await ensure_default_admin_exists(container)
        if admin_user:
            logger.info(f"Default admin user ready: {admin_user.username}")
        else:
            logger.info("Default admin user check complete (already exists or skipped)")

    logger.info("Application bootstrap complete")
