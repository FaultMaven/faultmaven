"""MultiTenantProvider for Cloud/Enterprise Deployments.

Requires explicit organization_id for each request and validates user membership.
Enforces multi-tenant isolation with strict authorization checks.

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

from faultmaven.exceptions import AuthorizationError, NotFoundError, ValidationException
from faultmaven.models.interfaces_user import IOrganizationRepository, Organization
from faultmaven.models.user import User
from faultmaven.providers.tenancy.base import TenantProvider


class MultiTenantProvider(TenantProvider):
    """Multi-tenant provider for cloud/enterprise deployments.

    Behavior:
    - Requires explicit organization_id for each request
    - Validates user membership in organization
    - Enforces multi-tenant isolation

    Use Cases:
    - Cloud SaaS deployment (multiple organizations)
    - Enterprise deployment (department isolation)
    - Production environments

    Design Notes:
        Organization ID is typically extracted from:
        1. JWT claim (preferred for API requests)
        2. X-Organization-ID header (browser extensions)
    """

    def __init__(self, organization_repository: IOrganizationRepository):
        """Initialize multi-tenant provider.

        Args:
            organization_repository: Repository for organization persistence
        """
        self.organization_repository = organization_repository

    async def get_current_organization(
        self, current_user: User, organization_id: str | None = None
    ) -> Organization:
        """Get organization with membership validation.

        Args:
            current_user: Authenticated user
            organization_id: Required in multi-tenant mode

        Returns:
            Organization: The organization if user is a member

        Raises:
            ValidationException: If organization_id not provided
            NotFoundError: If organization doesn't exist
            AuthorizationError: If user not a member

        Design Notes:
            Uses IOrganizationRepository.get_member_role() to verify membership.
            The role_id is checked for existence (not None) rather than specific
            permission validation, which is handled by permission checks elsewhere.
        """
        if not organization_id:
            raise ValidationException(
                "organization_id is required in multi-tenant mode. "
                "Provide via JWT claim or X-Organization-ID header.",
                details={
                    "tenant_provider": "multi",
                    "user_id": current_user.user_id,
                    "hint": "Add organization_id to request context",
                },
            )

        # Get organization
        organization = await self.organization_repository.get_organization(
            organization_id
        )
        if not organization:
            raise NotFoundError(
                resource_type="Organization", resource_id=organization_id
            )

        # Verify user membership using repository method
        user_role = await self.organization_repository.get_member_role(
            organization_id=organization_id, user_id=current_user.user_id
        )

        if user_role is None:
            raise AuthorizationError(
                f"User {current_user.email} is not a member of organization {organization.name}"
            )

        return organization

    async def get_default_organization(self) -> Organization:
        """Not supported in multi-tenant mode.

        Raises:
            NotImplementedError: Multi-tenant mode requires explicit organization_id

        Design Notes:
            This method exists for interface compatibility but should never
            be called in multi-tenant deployments. Services should always
            provide an explicit organization_id.
        """
        raise NotImplementedError(
            "Multi-tenant mode does not have a default organization. "
            "Provide organization_id explicitly via request context."
        )

    async def is_multi_tenant(self) -> bool:
        """Multi-tenant mode."""
        return True
