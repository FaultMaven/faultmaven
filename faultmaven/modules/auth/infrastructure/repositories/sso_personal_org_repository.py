"""Persistence adapter for personal tenants (#1045, ADR-016 D5).

Implements ``ISSOPersonalOrgRepository`` (``modules/auth/contracts.py``) over
``sso_personal_orgs`` (migration 051) plus the four rows a tenant is made of.
Both tables it keys on — that one and ``sso_org_mappings`` — are deliberately
outside RLS: they are read on the unauthenticated SSO callback, before a tenant
is bound.

Why this is not ``fm-provision-sso-org``
----------------------------------------
It writes the same rows in the same order — enterprise, organization, default
team, mapping — because the ordering constraints are the same. It differs in
exactly two ways, and both follow from the trigger being a *login* rather than
an operator:

**It runs under the RLS-scoped application role.** The CLI demands the
RLS-exempt owner DSN, and its module docstring explains why: it resolves an
organization by ``(enterprise_id, slug)``, an id-blind lookup that the
``organizations`` policy cannot satisfy. This path does not have that problem,
because it does not have that lookup. The caller *generates* the organization id
and binds it as the tenant context before the transaction opens, so the engine's
``begin`` listener writes it into ``app.current_org_id`` and every INSERT below
matches the policy — migration 018 creates its policies with no ``FOR`` clause,
which makes ``USING`` double as ``WITH CHECK``. The subject-keyed row is the
identity the CLI's slug lookup stands in for, and it is untenanted, so the
"which organization is this?" question is answered before RLS is in the way.

**Its refusals are conflicts, not alarms.** The CLI stops and asks a human when
a slug resolves onto an existing tenant, because an operator provisioning Acme
onto Beta's organization is a catastrophe. Here the only way to reach an
existing row is to *be* that subject or to race yourself, and the right answer
in both cases is to adopt what is already there.

Idempotency and races
---------------------
The whole write is one transaction, so it commits entirely or not at all. Two
concurrent first logins for the same subject therefore cannot both succeed:
they derive the same slug and the same IdP organization, so the loser trips one
of three constraints — ``enterprises.slug`` (unique globally),
``sso_org_mappings``'s primary key, or ``sso_personal_orgs``'s — and rolls back
whole, leaving no enterprise, organization or team behind. It then re-reads the
subject row and adopts the winner's tenant. There is no window in which a
half-built tenant is visible to anyone, because an uncommitted transaction is
visible to nobody.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from faultmaven.config.constants import STANDALONE_TEAM_NAME
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    SSOPersonalOrgModel,
    TeamModel,
)
from faultmaven.modules.auth.contracts import (
    ISSOPersonalOrgRepository,
    PersonalTenant,
)

logger = structlog.get_logger(__name__)

#: Every organization gets one team at creation (ADR-013). Aliased from the one
#: shared constant rather than re-spelled, so this path, the operator CLI and
#: the standalone bootstrap cannot drift into three "default" teams that differ
#: by a word — ``SingleTenantProvider`` aliases it the same way.
DEFAULT_TEAM_NAME = STANDALONE_TEAM_NAME


def _now() -> datetime:
    return datetime.now(UTC)


class SessionlessSSOPersonalOrgRepository(ISSOPersonalOrgRepository):
    """Sessionless personal-tenant lookup and provisioning.

    One database session per operation, matching the shape the composition root
    injects for the sibling mapping repository. That per-operation session is
    also what makes the caller's mid-flow ``set_current_org_id`` take effect:
    the engine's ``begin`` listener samples the contextvar once per
    transaction, so a rebind is only honoured by transactions opened after it
    (#935).
    """

    async def get_organization_id(
        self, provider: str, provider_user_id: str
    ) -> Optional[str]:
        """Return the subject's personal organization id, or None."""
        async with get_db_session() as session:
            stmt = select(SSOPersonalOrgModel.organization_id).where(
                SSOPersonalOrgModel.provider == provider,
                SSOPersonalOrgModel.provider_user_id == provider_user_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def provision(
        self,
        *,
        provider: str,
        provider_user_id: str,
        provider_org_id: str,
        organization_id: str,
        name: str,
        slug: str,
    ) -> PersonalTenant:
        """Create the subject's tenant atomically, or adopt an existing one."""
        existing = await self.get_organization_id(provider, provider_user_id)
        if existing is not None:
            return PersonalTenant(organization_id=existing, created=False)

        try:
            await self._write(
                provider=provider,
                provider_user_id=provider_user_id,
                provider_org_id=provider_org_id,
                organization_id=organization_id,
                name=name,
                slug=slug,
            )
        except IntegrityError:
            # Lost a race with a concurrent first login for the same subject.
            # The whole transaction rolled back, so nothing of ours survives;
            # the winner's tenant is the one this login wants.
            adopted = await self.get_organization_id(provider, provider_user_id)
            if adopted is not None:
                logger.info(
                    "sso_personal_tenant_race_adopted",
                    provider=provider,
                    organization_id=adopted,
                )
                return PersonalTenant(organization_id=adopted, created=False)
            # A constraint fired that the subject row does not explain — a slug
            # or IdP-organization collision with a tenant belonging to somebody
            # else. Refusing is the only safe answer: the alternative is landing
            # this login in a tenant it does not own.
            logger.error(
                "sso_personal_tenant_conflict_unresolved",
                provider=provider,
                organization_id=organization_id,
            )
            raise
        return PersonalTenant(organization_id=organization_id, created=True)

    async def _write(
        self,
        *,
        provider: str,
        provider_user_id: str,
        provider_org_id: str,
        organization_id: str,
        name: str,
        slug: str,
    ) -> None:
        """Write all five rows in one transaction, in the operator path's order."""
        now = _now()
        enterprise_id = str(uuid.uuid4())
        team_id = str(uuid.uuid4())

        async with get_db_session() as session:
            # 1. Enterprise — the top tier. Not RLS-tenanted (no
            #    ``organization_id`` column), so this write is unaffected by the
            #    bound scope; its globally-unique slug is nevertheless one of
            #    the three constraints that arbitrate a race.
            session.add(
                EnterpriseModel(
                    enterprise_id=enterprise_id,
                    name=name,
                    slug=slug,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

            # 2. The organization — the isolation boundary. Its id was chosen by
            #    the caller and bound as the tenant context before this
            #    transaction opened, which is what lets the RLS policy's WITH
            #    CHECK arm accept the row under the application role.
            session.add(
                OrganizationModel(
                    organization_id=organization_id,
                    enterprise_id=enterprise_id,
                    name=name,
                    slug=slug,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

            # 3. The default team (ADR-013: every organization has one).
            session.add(
                TeamModel(
                    team_id=team_id,
                    organization_id=organization_id,
                    name=DEFAULT_TEAM_NAME,
                    description="Default team for this organization",
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

            # 4. The IdP-organization binding, so a later login that DOES carry
            #    an organization resolves through the ordinary mapped path to
            #    this same tenant.
            session.add(
                SSOOrgMappingModel(
                    provider=provider,
                    provider_org_id=provider_org_id,
                    organization_id=organization_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

            # 5. The subject binding — the row that answers "where does this
            #    individual live?" on every later login, including the ones the
            #    IdP reports no organization for. Written last so that on a
            #    race it is the final arbiter, and so nothing above it is
            #    visible without it.
            session.add(
                SSOPersonalOrgModel(
                    provider=provider,
                    provider_user_id=provider_user_id,
                    organization_id=organization_id,
                    provider_org_id=provider_org_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

        logger.info(
            "sso_personal_tenant_provisioned",
            provider=provider,
            organization_id=organization_id,
            enterprise_id=enterprise_id,
        )
