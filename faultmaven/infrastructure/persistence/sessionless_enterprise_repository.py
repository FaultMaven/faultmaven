"""Sessionless Enterprise Repository.

Wrapper around PostgreSQLEnterpriseRepository that creates sessions
per operation using ``get_db_session()``, mirroring
SessionlessOrganizationRepository. This lets the DI container hand a
single repository instance to SingleTenantProvider without holding a
long-lived AsyncSession.
"""

from typing import Optional

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.enterprise_repository import (
    PostgreSQLEnterpriseRepository,
)
from faultmaven.models.interfaces_user import Enterprise, IEnterpriseRepository


class SessionlessEnterpriseRepository(IEnterpriseRepository):
    """Sessionless wrapper for enterprise repository.

    Creates a new database session for each operation using
    ``get_db_session()``. Same pattern as
    SessionlessOrganizationRepository.
    """

    async def create_enterprise(self, enterprise: Enterprise) -> Enterprise:
        async with get_db_session() as session:
            repo = PostgreSQLEnterpriseRepository(session)
            return await repo.create_enterprise(enterprise)

    async def get_enterprise(self, enterprise_id: str) -> Optional[Enterprise]:
        async with get_db_session() as session:
            repo = PostgreSQLEnterpriseRepository(session)
            return await repo.get_enterprise(enterprise_id)

    async def get_enterprise_by_slug(self, slug: str) -> Optional[Enterprise]:
        async with get_db_session() as session:
            repo = PostgreSQLEnterpriseRepository(session)
            return await repo.get_enterprise_by_slug(slug)

    async def update_enterprise(self, enterprise: Enterprise) -> bool:
        async with get_db_session() as session:
            repo = PostgreSQLEnterpriseRepository(session)
            return await repo.update_enterprise(enterprise)
