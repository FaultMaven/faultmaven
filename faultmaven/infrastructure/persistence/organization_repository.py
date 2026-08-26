"""Organization Repository - SQLAlchemy ORM Implementation.

Implements IOrganizationRepository for organization and member management.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Union

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.db_compat import dialect_insert
from faultmaven.infrastructure.persistence.models import (
    OrganizationMemberModel,
    OrganizationModel,
    RolePermissionModel,
)
from faultmaven.models.interfaces_user import (
    IOrganizationRepository,
    Organization,
    OrganizationMember,
    OrgPlanTier,
)
from faultmaven.models.rbac import Permission

logger = logging.getLogger(__name__)

#: The constraint trigger migration 044 (``c7d8e9f0a1b2``) installs on
#: ``organization_members``: it refuses any write that would leave an
#: organization with no admin (fm#1161). PostgreSQL reports it as SQLSTATE
#: ``23514`` carrying this name in the error's ``constraint_name`` field.
#:
#: The migration holds a frozen copy of this string, per the convention that a
#: migration states the values it was written against rather than importing
#: runtime code. ``tests/unit/infrastructure/persistence/test_last_admin_guard.py``
#: asserts the two agree.
LAST_ADMIN_CONSTRAINT = "organization_members_last_admin"

#: SQLSTATE 23514, ``check_violation``.
_CHECK_VIOLATION = "23514"


def is_last_admin_violation(exc: BaseException) -> bool:
    """Is ``exc`` the last-admin constraint trigger refusing a write?

    Callers that already refuse this in application code — the Cloud
    org-management service — use this to turn the database's refusal into the
    same friendly error, so the rare path where the trigger is the one that
    catches it does not surface as an unhandled database fault.

    Identified by the structured fields PostgreSQL sends rather than by message
    text: matching the message would break the day someone rewords it, and
    matching only the error class would swallow every other constraint on the
    table. ``exc.orig`` is SQLAlchemy's DBAPI wrapper; the driver exception
    carrying the fields is its ``__cause__``.

    Returns ``False`` for anything else, including on SQLite, where the trigger
    does not exist — Standalone is single-tenant and has no organizations to
    orphan.
    """
    if not isinstance(exc, DBAPIError):
        return False
    cause = getattr(exc.orig, "__cause__", None)
    return (
        getattr(cause, "sqlstate", None) == _CHECK_VIOLATION
        and getattr(cause, "constraint_name", None) == LAST_ADMIN_CONSTRAINT
    )


def _parse_settings(raw) -> dict:
    """Parse settings from DB (TEXT in SQLite, JSONB in PostgreSQL)."""
    if isinstance(raw, str):
        return json.loads(raw) if raw else {}
    if raw is None:
        return {}
    return raw


def _model_to_domain(model: OrganizationModel) -> Organization:
    """Convert ORM model to domain object.

    plan_tier / max_members / max_cases / settings live on the parent
    enterprise (`enterprises` table). The Organization domain object
    falls back to Pydantic defaults for those fields here; callers
    that need plan-tier semantics should resolve them via the
    enterprise repository.

    ``is_active`` IS carried: the SSO org-mapping login path refuses to land a
    user in a deactivated organization (#869), and a gate over a field the
    mapper dropped would be a gate that can never fire.
    """
    return Organization(
        organization_id=model.organization_id,
        enterprise_id=model.enterprise_id,
        name=model.name,
        slug=model.slug,
        description=model.description,
        is_active=bool(model.is_active),
        created_at=model.created_at,
        updated_at=model.updated_at,
        deleted_at=model.deleted_at,
    )


class PostgreSQLOrganizationRepository(IOrganizationRepository):
    """SQLAlchemy ORM implementation of organization repository."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def create_organization(self, org: Organization) -> Organization:
        """Create a new organization.

        plan_tier / max_members / max_cases / settings are dropped from
        the persistence write — they belong on the parent enterprise
        (`enterprises` table). enterprise_id must be set on the domain
        object; the column is NOT NULL.
        """
        if not org.enterprise_id:
            from faultmaven.providers.tenancy.single_tenant import (
                DEFAULT_ENTERPRISE_ID,
            )

            org.enterprise_id = DEFAULT_ENTERPRISE_ID
        model = OrganizationModel(
            organization_id=org.organization_id,
            enterprise_id=org.enterprise_id,
            name=org.name,
            slug=org.slug,
            description=org.description,
            created_at=org.created_at,
            updated_at=org.updated_at,
        )
        self.db.add(model)
        await self.db.commit()

        logger.info(f"Created organization: {org.organization_id} ({org.name})")
        return org

    async def get_organization(self, organization_id: str) -> Optional[Organization]:
        """Get organization by ID."""
        stmt = select(OrganizationModel).where(
            OrganizationModel.organization_id == organization_id,
            OrganizationModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug."""
        stmt = select(OrganizationModel).where(
            OrganizationModel.slug == slug,
            OrganizationModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def update_organization(self, org: Organization) -> bool:
        """Update organization."""
        org.updated_at = datetime.now(timezone.utc)

        stmt = (
            update(OrganizationModel)
            .where(
                OrganizationModel.organization_id == org.organization_id,
                OrganizationModel.deleted_at.is_(None),
            )
            .values(
                name=org.name,
                slug=org.slug,
                description=org.description,
                updated_at=org.updated_at,
            )
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def delete_organization(self, organization_id: str) -> bool:
        """Soft delete organization."""
        stmt = (
            update(OrganizationModel)
            .where(
                OrganizationModel.organization_id == organization_id,
                OrganizationModel.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(timezone.utc))
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def list_user_organizations(self, user_id: str) -> List[Organization]:
        """List all organizations a user belongs to."""
        stmt = (
            select(OrganizationModel)
            .join(
                OrganizationMemberModel,
                OrganizationModel.organization_id
                == OrganizationMemberModel.organization_id,
            )
            .where(
                OrganizationMemberModel.user_id == user_id,
                OrganizationModel.deleted_at.is_(None),
            )
            .order_by(OrganizationMemberModel.joined_at.desc())
        )
        result = await self.db.execute(stmt)
        models = result.scalars().all()
        return [_model_to_domain(m) for m in models]

    async def add_member(
        self, organization_id: str, user_id: str, role_id: str
    ) -> bool:
        """Add user to organization with role (upsert)."""
        now = datetime.now(timezone.utc)
        stmt = dialect_insert(self.db, OrganizationMemberModel).values(
            user_id=user_id,
            organization_id=organization_id,
            role_id=role_id,
            joined_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "organization_id"],
            set_={"role_id": role_id, "updated_at": now},
        )
        await self.db.execute(stmt)
        await self.db.commit()

        logger.info(
            f"Added user {user_id} to organization {organization_id} with role {role_id}"
        )
        return True

    async def remove_member(self, organization_id: str, user_id: str) -> bool:
        """Remove user from organization."""
        stmt = delete(OrganizationMemberModel).where(
            OrganizationMemberModel.organization_id == organization_id,
            OrganizationMemberModel.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def update_member_role(
        self, organization_id: str, user_id: str, role_id: str
    ) -> bool:
        """Update user's role in organization."""
        stmt = (
            update(OrganizationMemberModel)
            .where(
                OrganizationMemberModel.organization_id == organization_id,
                OrganizationMemberModel.user_id == user_id,
            )
            .values(role_id=role_id)
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount > 0

    async def list_organization_members(
        self, organization_id: str
    ) -> List[OrganizationMember]:
        """List all members of an organization."""
        stmt = (
            select(OrganizationMemberModel)
            .where(OrganizationMemberModel.organization_id == organization_id)
            .order_by(OrganizationMemberModel.joined_at.desc())
        )
        result = await self.db.execute(stmt)
        rows = result.scalars().all()

        return [
            OrganizationMember(
                user_id=row.user_id,
                organization_id=row.organization_id,
                role_id=row.role_id,
                joined_at=row.joined_at,
                last_active_at=row.last_active_at,
            )
            for row in rows
        ]

    async def get_member_role(
        self, organization_id: str, user_id: str
    ) -> Optional[str]:
        """Get user's role in organization."""
        stmt = select(OrganizationMemberModel.role_id).where(
            OrganizationMemberModel.organization_id == organization_id,
            OrganizationMemberModel.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one_or_none()
        return row if row else None

    async def user_has_permission(
        self, user_id: str, organization_id: str, permission: Union[Permission, str]
    ) -> bool:
        """Check if user has permission in organization.

        Replaces the old SQL function user_has_org_permission() with an ORM join.

        The permission is spelled the way :class:`~faultmaven.models.rbac.Permission`
        spells it — ``resource:action`` with a **colon** — and a ``Permission``
        member may be passed directly. That is also how migration 029 seeds the
        ``permissions`` rows, so caller, enum and table now agree on one
        spelling. The old ``resource.action`` (dot) form this method used to
        parse is **no longer accepted**: it was unreachable from the enum's own
        value, so passing ``Permission.ORG_MANAGE_USERS`` silently denied.

        Anything that is not exactly one ``resource:action`` pair returns
        ``False`` — an authorization primitive fails closed on input it cannot
        interpret.
        """
        raw = permission.value if isinstance(permission, Permission) else permission
        if not isinstance(raw, str):
            return False
        parts = raw.split(":")
        if len(parts) != 2 or not all(parts):
            return False
        resource, action = parts

        from faultmaven.infrastructure.persistence.models import PermissionModel

        stmt = (
            select(func.count())
            .select_from(OrganizationMemberModel)
            .join(
                RolePermissionModel,
                OrganizationMemberModel.role_id == RolePermissionModel.role_id,
            )
            .join(
                PermissionModel,
                RolePermissionModel.permission_id == PermissionModel.permission_id,
            )
            .where(
                OrganizationMemberModel.user_id == user_id,
                OrganizationMemberModel.organization_id == organization_id,
                PermissionModel.resource == resource,
                PermissionModel.action == action,
            )
        )
        result = await self.db.execute(stmt)
        count = result.scalar()
        return count > 0
