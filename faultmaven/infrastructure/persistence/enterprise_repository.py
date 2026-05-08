"""Enterprise Repository - SQLAlchemy ORM Implementation.

Implements IEnterpriseRepository for the top-tier tenant (Enterprise),
which owns SSO/SAML config, billing, plan tier, and contains
organizations.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from faultmaven.infrastructure.persistence.models import EnterpriseModel
from faultmaven.models.interfaces_user import (
    Enterprise,
    EnterprisePlanTier,
    IEnterpriseRepository,
)

logger = logging.getLogger(__name__)


def _parse_settings(raw) -> dict:
    """Parse settings from DB (TEXT in SQLite, JSONB in PostgreSQL)."""
    if isinstance(raw, str):
        return json.loads(raw) if raw else {}
    if raw is None:
        return {}
    return raw


def _serialize_settings(settings: dict) -> str:
    """Serialize settings dict for JsonBlob (TEXT on SQLite, JSONB on PG)."""
    return json.dumps(settings or {})


def _model_to_domain(model: EnterpriseModel) -> Enterprise:
    return Enterprise(
        enterprise_id=model.enterprise_id,
        name=model.name,
        slug=model.slug,
        plan_tier=EnterprisePlanTier(model.plan_tier),
        max_members=model.max_members,
        max_cases=model.max_cases,
        billing_email=model.billing_email,
        settings=_parse_settings(model.settings),
        created_at=model.created_at,
        updated_at=model.updated_at,
        deleted_at=model.deleted_at,
    )


class PostgreSQLEnterpriseRepository(IEnterpriseRepository):
    """SQLAlchemy ORM enterprise repository.

    Despite the name, works with both SQLite and PostgreSQL via the
    SQLAlchemy abstraction (matches the convention used by
    PostgreSQLOrganizationRepository / PostgreSQLUserRepository).
    """

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def create_enterprise(self, enterprise: Enterprise) -> Enterprise:
        model = EnterpriseModel(
            enterprise_id=enterprise.enterprise_id,
            name=enterprise.name,
            slug=enterprise.slug,
            plan_tier=enterprise.plan_tier.value,
            max_members=enterprise.max_members,
            max_cases=enterprise.max_cases,
            billing_email=enterprise.billing_email,
            settings=_serialize_settings(enterprise.settings),
            created_at=enterprise.created_at,
            updated_at=enterprise.updated_at,
        )
        self.db.add(model)
        await self.db.commit()
        logger.info(
            "Created enterprise: %s (%s)",
            enterprise.enterprise_id,
            enterprise.name,
        )
        return enterprise

    async def get_enterprise(self, enterprise_id: str) -> Optional[Enterprise]:
        stmt = select(EnterpriseModel).where(
            EnterpriseModel.enterprise_id == enterprise_id,
            EnterpriseModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def get_enterprise_by_slug(self, slug: str) -> Optional[Enterprise]:
        stmt = select(EnterpriseModel).where(
            EnterpriseModel.slug == slug,
            EnterpriseModel.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        return _model_to_domain(model) if model else None

    async def update_enterprise(self, enterprise: Enterprise) -> bool:
        enterprise.updated_at = datetime.now(timezone.utc)
        stmt = (
            update(EnterpriseModel)
            .where(
                EnterpriseModel.enterprise_id == enterprise.enterprise_id,
                EnterpriseModel.deleted_at.is_(None),
            )
            .values(
                name=enterprise.name,
                slug=enterprise.slug,
                plan_tier=enterprise.plan_tier.value,
                max_members=enterprise.max_members,
                max_cases=enterprise.max_cases,
                billing_email=enterprise.billing_email,
                settings=_serialize_settings(enterprise.settings),
                updated_at=enterprise.updated_at,
            )
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return (result.rowcount or 0) > 0
