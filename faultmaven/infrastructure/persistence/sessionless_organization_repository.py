"""Sessionless Organization Repository.

Wrapper around PostgreSQLOrganizationRepository that creates sessions per operation
using get_db_session() context manager, following the same pattern as SessionlessCaseRepository.

This removes the need for a long-lived db_session in the DI container.
"""

from typing import List, Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.organization_repository import (
    PostgreSQLOrganizationRepository,
)
from faultmaven.models.interfaces_user import (
    IOrganizationRepository,
    Organization,
    OrganizationMember,
)


class SessionlessOrganizationRepository(IOrganizationRepository):
    """Sessionless wrapper for organization repository.

    Creates a new database session for each operation using get_db_session().
    """

    async def get_organization(self, organization_id: str) -> Optional[Organization]:
        """Get organization by ID."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.get_organization(organization_id)

    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.get_organization_by_slug(slug)

    async def list_user_organizations(self, user_id: str) -> List[Organization]:
        """List all organizations a user belongs to."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.list_user_organizations(user_id)

    async def create_organization(self, organization: Organization) -> Organization:
        """Create a new organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.create_organization(organization)

    async def update_organization(self, organization: Organization) -> Organization:
        """Update an existing organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.update_organization(organization)

    async def delete_organization(self, organization_id: str) -> bool:
        """Soft delete an organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.delete_organization(organization_id)

    async def add_member(
        self, organization_id: str, user_id: str, role_id: str
    ) -> bool:
        """Add user to organization with role."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.add_member(organization_id, user_id, role_id)

    async def remove_member(self, organization_id: str, user_id: str) -> bool:
        """Remove user from organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.remove_member(organization_id, user_id)

    async def update_member_role(
        self, organization_id: str, user_id: str, role_id: str
    ) -> bool:
        """Update user's role in organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.update_member_role(organization_id, user_id, role_id)

    async def list_organization_members(
        self, organization_id: str
    ) -> List[OrganizationMember]:
        """List all members of an organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.list_organization_members(organization_id)

    async def get_member_role(
        self, organization_id: str, user_id: str
    ) -> Optional[str]:
        """Get user's role in organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.get_member_role(organization_id, user_id)

    async def user_has_permission(
        self, user_id: str, organization_id: str, permission: str
    ) -> bool:
        """Check if user has permission in organization."""
        async with get_db_session() as session:
            repo = PostgreSQLOrganizationRepository(session)
            return await repo.user_has_permission(user_id, organization_id, permission)
