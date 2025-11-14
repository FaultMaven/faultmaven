"""Organization Repository - PostgreSQL Implementation.

Implements IOrganizationRepository for organization and member management.
"""

from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from faultmaven.models.interfaces_user import (
    IOrganizationRepository,
    Organization,
    OrganizationMember,
    OrgPlanTier
)

logger = logging.getLogger(__name__)


class PostgreSQLOrganizationRepository(IOrganizationRepository):
    """PostgreSQL implementation of organization repository.

    Manages organizations, members, and RBAC permissions.
    """

    def __init__(self, db_session: AsyncSession):
        """Initialize repository with database session.

        Args:
            db_session: SQLAlchemy async session
        """
        self.db = db_session

    async def create_organization(self, org: Organization) -> Organization:
        """Create a new organization."""
        query = text("""
            INSERT INTO organizations (
                org_id, name, slug, description, plan_tier, max_members, max_cases,
                settings, created_at, updated_at
            ) VALUES (
                :org_id, :name, :slug, :description, :plan_tier, :max_members, :max_cases,
                :settings::jsonb, :created_at, :updated_at
            )
            RETURNING org_id
        """)

        await self.db.execute(query, {
            "org_id": org.org_id,
            "name": org.name,
            "slug": org.slug,
            "description": org.description,
            "plan_tier": org.plan_tier.value,
            "max_members": org.max_members,
            "max_cases": org.max_cases,
            "settings": org.settings or {},
            "created_at": org.created_at,
            "updated_at": org.updated_at
        })
        await self.db.commit()

        logger.info(f"Created organization: {org.org_id} ({org.name})")
        return org

    async def get_organization(self, org_id: str) -> Optional[Organization]:
        """Get organization by ID."""
        query = text("""
            SELECT org_id, name, slug, description, plan_tier, max_members, max_cases,
                   settings, created_at, updated_at, deleted_at
            FROM organizations
            WHERE org_id = :org_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {"org_id": org_id})
        row = result.fetchone()

        if not row:
            return None

        return Organization(
            org_id=row.org_id,
            name=row.name,
            slug=row.slug,
            description=row.description,
            plan_tier=OrgPlanTier(row.plan_tier),
            max_members=row.max_members,
            max_cases=row.max_cases,
            settings=row.settings or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at
        )

    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug."""
        query = text("""
            SELECT org_id, name, slug, description, plan_tier, max_members, max_cases,
                   settings, created_at, updated_at, deleted_at
            FROM organizations
            WHERE slug = :slug AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {"slug": slug})
        row = result.fetchone()

        if not row:
            return None

        return Organization(
            org_id=row.org_id,
            name=row.name,
            slug=row.slug,
            description=row.description,
            plan_tier=OrgPlanTier(row.plan_tier),
            max_members=row.max_members,
            max_cases=row.max_cases,
            settings=row.settings or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at
        )

    async def update_organization(self, org: Organization) -> bool:
        """Update organization."""
        org.updated_at = datetime.now(timezone.utc)

        query = text("""
            UPDATE organizations
            SET name = :name,
                slug = :slug,
                description = :description,
                plan_tier = :plan_tier,
                max_members = :max_members,
                max_cases = :max_cases,
                settings = :settings::jsonb,
                updated_at = :updated_at
            WHERE org_id = :org_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {
            "org_id": org.org_id,
            "name": org.name,
            "slug": org.slug,
            "description": org.description,
            "plan_tier": org.plan_tier.value,
            "max_members": org.max_members,
            "max_cases": org.max_cases,
            "settings": org.settings or {},
            "updated_at": org.updated_at
        })
        await self.db.commit()

        return result.rowcount > 0

    async def delete_organization(self, org_id: str) -> bool:
        """Soft delete organization."""
        query = text("""
            UPDATE organizations
            SET deleted_at = :deleted_at
            WHERE org_id = :org_id AND deleted_at IS NULL
        """)

        result = await self.db.execute(query, {
            "org_id": org_id,
            "deleted_at": datetime.now(timezone.utc)
        })
        await self.db.commit()

        return result.rowcount > 0

    async def list_user_organizations(self, user_id: str) -> List[Organization]:
        """List all organizations a user belongs to."""
        query = text("""
            SELECT o.org_id, o.name, o.slug, o.description, o.plan_tier, o.max_members,
                   o.max_cases, o.settings, o.created_at, o.updated_at, o.deleted_at
            FROM organizations o
            JOIN organization_members om ON o.org_id = om.org_id
            WHERE om.user_id = :user_id AND o.deleted_at IS NULL
            ORDER BY om.joined_at DESC
        """)

        result = await self.db.execute(query, {"user_id": user_id})
        rows = result.fetchall()

        return [
            Organization(
                org_id=row.org_id,
                name=row.name,
                slug=row.slug,
                description=row.description,
                plan_tier=OrgPlanTier(row.plan_tier),
                max_members=row.max_members,
                max_cases=row.max_cases,
                settings=row.settings or {},
                created_at=row.created_at,
                updated_at=row.updated_at,
                deleted_at=row.deleted_at
            )
            for row in rows
        ]

    async def add_member(self, org_id: str, user_id: str, role_id: str) -> bool:
        """Add user to organization with role."""
        query = text("""
            INSERT INTO organization_members (user_id, org_id, role_id, joined_at)
            VALUES (:user_id, :org_id, :role_id, :joined_at)
            ON CONFLICT (user_id, org_id) DO UPDATE
            SET role_id = EXCLUDED.role_id
        """)

        await self.db.execute(query, {
            "user_id": user_id,
            "org_id": org_id,
            "role_id": role_id,
            "joined_at": datetime.now(timezone.utc)
        })
        await self.db.commit()

        logger.info(f"Added user {user_id} to organization {org_id} with role {role_id}")
        return True

    async def remove_member(self, org_id: str, user_id: str) -> bool:
        """Remove user from organization."""
        query = text("""
            DELETE FROM organization_members
            WHERE org_id = :org_id AND user_id = :user_id
        """)

        result = await self.db.execute(query, {"org_id": org_id, "user_id": user_id})
        await self.db.commit()

        return result.rowcount > 0

    async def update_member_role(self, org_id: str, user_id: str, role_id: str) -> bool:
        """Update user's role in organization."""
        query = text("""
            UPDATE organization_members
            SET role_id = :role_id
            WHERE org_id = :org_id AND user_id = :user_id
        """)

        result = await self.db.execute(query, {
            "org_id": org_id,
            "user_id": user_id,
            "role_id": role_id
        })
        await self.db.commit()

        return result.rowcount > 0

    async def list_organization_members(self, org_id: str) -> List[OrganizationMember]:
        """List all members of an organization."""
        query = text("""
            SELECT user_id, org_id, role_id, joined_at, last_active_at
            FROM organization_members
            WHERE org_id = :org_id
            ORDER BY joined_at DESC
        """)

        result = await self.db.execute(query, {"org_id": org_id})
        rows = result.fetchall()

        return [
            OrganizationMember(
                user_id=row.user_id,
                org_id=row.org_id,
                role_id=row.role_id,
                joined_at=row.joined_at,
                last_active_at=row.last_active_at
            )
            for row in rows
        ]

    async def get_member_role(self, org_id: str, user_id: str) -> Optional[str]:
        """Get user's role in organization."""
        query = text("""
            SELECT role_id
            FROM organization_members
            WHERE org_id = :org_id AND user_id = :user_id
        """)

        result = await self.db.execute(query, {"org_id": org_id, "user_id": user_id})
        row = result.fetchone()

        return row.role_id if row else None

    async def user_has_permission(
        self,
        user_id: str,
        org_id: str,
        permission: str
    ) -> bool:
        """Check if user has permission in organization.

        Uses the SQL function created in migration 003.
        Permission format: 'resource.action' (e.g., 'cases.write')
        """
        query = text("""
            SELECT user_has_org_permission(:user_id, :org_id, :permission)
        """)

        result = await self.db.execute(query, {
            "user_id": user_id,
            "org_id": org_id,
            "permission": permission
        })
        row = result.fetchone()

        return bool(row[0]) if row else False
