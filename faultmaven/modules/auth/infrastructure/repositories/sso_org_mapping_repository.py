"""Persistence adapter for the IdP-organization → FaultMaven-enterprise map.

Implements ``ISSOOrgMappingRepository`` (``modules/auth/contracts.py``) over the
``sso_org_mappings`` table. It targets the ENTERPRISE (ADR-017 D9): a company
that already has an IdP organization maps it to its enterprise, and its members
land there in no organization until somebody pays for them. The table is
deliberately outside RLS: it is read on the unauthenticated SSO callback, before
a tenant is bound.

Two shapes, mirroring the tenancy repositories: a session-bound repository
for callers that already own a transaction, and a sessionless wrapper that opens
one session per operation — the shape the composition root injects into
``SSOLoginService``.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import SSOOrgMappingModel
from faultmaven.modules.auth.contracts import ISSOOrgMappingRepository


class SSOOrgMappingRepository(ISSOOrgMappingRepository):
    """Session-bound mapping lookup."""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def get_enterprise_id(
        self, provider: str, provider_org_id: str
    ) -> Optional[str]:
        """Return the mapped FaultMaven enterprise id, or None if unmapped."""
        stmt = select(SSOOrgMappingModel.enterprise_id).where(
            SSOOrgMappingModel.provider == provider,
            SSOOrgMappingModel.provider_org_id == provider_org_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()


class SessionlessSSOOrgMappingRepository(ISSOOrgMappingRepository):
    """Sessionless wrapper: one database session per operation."""

    async def get_enterprise_id(
        self, provider: str, provider_org_id: str
    ) -> Optional[str]:
        """Return the mapped FaultMaven enterprise id, or None if unmapped."""
        async with get_db_session() as session:
            return await SSOOrgMappingRepository(session).get_enterprise_id(
                provider, provider_org_id
            )
