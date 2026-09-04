"""Persistence adapter for personal tenants (#1045, ADR-016 D5).

Implements ``ISSOPersonalOrgRepository`` over ``sso_personal_orgs``
(migration 051). Both tables this path keys on — that one and
``sso_org_mappings`` — are deliberately outside RLS: they are read on the
unauthenticated SSO callback, before a tenant is bound.

The four tenant rows themselves are written by the shared
``infrastructure/persistence/tenant_bootstrap`` writer, the same one
``fm-provision-sso-org`` uses, so the ordering constraints cannot drift between
the operator path and the login path. What this module adds is the subject row,
the tenant binding, and the recovery semantics a login needs and an operator
does not.

Why this path does not need the operator's RLS-exempt role
----------------------------------------------------------
``fm-provision-sso-org`` demands the owner DSN because it resolves an
organization by ``(enterprise_id, slug)`` — an id-blind lookup the
``organizations`` policy cannot satisfy. This path has no such lookup: the
untenanted subject row answers "which organization?" before RLS is in the way,
and on a first sign-in there is no organization to find because this call is
what creates it. It therefore *generates* the id and binds it before opening the
transaction, so the engine's ``begin`` listener writes it into
``app.current_org_id`` and migration 018's policy (no ``FOR`` clause, so
``USING`` doubles as ``WITH CHECK``) accepts every row.

**The binding happens here, not in the caller.** A caller of :meth:`provision`
should not have to know that persisting a row requires a contextvar to be set
first — and a caller that knew could also forget, or leave a nonexistent
organization bound after a failed attempt. On every exit path this module
restores whatever scope it was called with; the login service rebinds to the
tenant it actually resolved, afterwards and once.

Idempotency, races, and collisions that are not races
------------------------------------------------------
The whole write is one transaction, so it commits entirely or not at all. Two
concurrent first logins for the same subject cannot both succeed: they derive
the same slug and the same IdP organization, so the loser trips one of the
constraints and rolls back whole, leaving no enterprise, organization or team
behind. It then re-reads the subject row and adopts the winner's tenant.

A constraint violation is **not** automatically a lost race, and conflating the
two produced a permanent lockout with a log that named the wrong thing (#1045
review, item 4). So when the subject row does not explain the violation, this
module asks the database *which key collided* and says so. The enterprise arm of
that class is gone by construction: the shared writer adopts an existing
enterprise with the same slug rather than always inserting, which is safe
precisely because the slug is derived from the subject and nobody else can
produce it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Optional

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from faultmaven.config.tenant_context import get_current_org_id, set_current_org_id
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    SSOPersonalOrgModel,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    OrgAlreadyClaimed,
    RemapRefused,
    bootstrap_tenant,
)
from faultmaven.modules.auth.contracts import (
    ISSOPersonalOrgRepository,
    PersonalOrgRecord,
)

logger = structlog.get_logger(__name__)

#: Everything that means "a key this attempt derived is already taken". The raw
#: ``IntegrityError`` covers the keys the database arbitrates (the subject row's
#: primary key, the enterprise slug); the two typed refusals cover the mapping
#: relation, which the shared writer checks before writing so the operator path
#: gets a named error instead of a raw constraint. All three are ambiguous in
#: the same way — a lost race or somebody else's key — and are disambiguated the
#: same way, by re-reading the untenanted subject row.
_CONFLICT_SIGNALS = (IntegrityError, RemapRefused, OrgAlreadyClaimed)


class PersonalTenantCollision(Exception):
    """A constraint fired that a lost race does not explain.

    Carries the key that actually collided, so the log names the thing an
    operator has to look at rather than the organization id this attempt
    invented and never committed.
    """

    def __init__(self, colliding_key: str, colliding_value: str) -> None:
        super().__init__(f"{colliding_key}={colliding_value}")
        self.colliding_key = colliding_key
        self.colliding_value = colliding_value


class SessionlessSSOPersonalOrgRepository(ISSOPersonalOrgRepository):
    """One database session per operation, as the sibling mapping repository."""

    async def get(
        self, provider: str, provider_user_id: str
    ) -> Optional[PersonalOrgRecord]:
        async with get_db_session() as session:
            row = await session.get(SSOPersonalOrgModel, (provider, provider_user_id))
            if row is None:
                return None
            return PersonalOrgRecord(
                organization_id=row.organization_id,
                enterprise_id=row.enterprise_id,
                provider_org_id=row.provider_org_id,
                membership_confirmed=bool(row.membership_confirmed),
            )

    async def find_by_enterprise(
        self, provider: str, provider_user_id: str, enterprise_id: str
    ) -> bool:
        record = await self.get(provider, provider_user_id)
        return record is not None and record.enterprise_id == enterprise_id

    async def count_created_since(self, provider: str, since: datetime) -> int:
        async with get_db_session() as session:
            stmt = select(func.count()).where(
                SSOPersonalOrgModel.provider == provider,
                SSOPersonalOrgModel.created_at >= since,
            )
            return int((await session.execute(stmt)).scalar_one())

    async def confirm_membership(self, provider: str, provider_user_id: str) -> None:
        async with get_db_session() as session:
            await session.execute(
                update(SSOPersonalOrgModel)
                .where(
                    SSOPersonalOrgModel.provider == provider,
                    SSOPersonalOrgModel.provider_user_id == provider_user_id,
                )
                .values(membership_confirmed=True, updated_at=datetime.now(UTC))
            )

    async def retire(self, provider: str, provider_user_id: str) -> bool:
        """Drop the binding. The organization and its cases are left in place."""
        async with get_db_session() as session:
            row = await session.get(SSOPersonalOrgModel, (provider, provider_user_id))
            if row is None:
                return False
            await session.delete(row)
        logger.info("sso_personal_tenant_retired", provider=provider)
        return True

    async def provision(
        self,
        *,
        provider: str,
        provider_user_id: str,
        provider_org_id: str,
        name: str,
        slug: str,
    ) -> str:
        """Create the subject's tenant atomically, or adopt an existing one."""
        organization_id = str(uuid.uuid4())
        restore_to = get_current_org_id()
        set_current_org_id(organization_id)
        try:
            await self._write(
                provider=provider,
                provider_user_id=provider_user_id,
                provider_org_id=provider_org_id,
                organization_id=organization_id,
                name=name,
                slug=slug,
            )
        except _CONFLICT_SIGNALS:
            # The whole transaction rolled back, so nothing of ours survives.
            # Either a concurrent login for the same subject won — in which case
            # its tenant is the one this login wants — or something else owns a
            # key we derived, which is a different problem with a different
            # remedy and must not be logged as a race.
            adopted = await self.get(provider, provider_user_id)
            if adopted is not None:
                logger.info(
                    "sso_personal_tenant_race_adopted",
                    provider=provider,
                    organization_id=adopted.organization_id,
                )
                return adopted.organization_id
            collision = await self._diagnose_collision(
                provider=provider, provider_org_id=provider_org_id, slug=slug
            )
            logger.error(
                "sso_personal_tenant_collision",
                provider=provider,
                colliding_key=collision.colliding_key,
                colliding_value=collision.colliding_value,
            )
            raise collision from None
        finally:
            set_current_org_id(restore_to)

        logger.info(
            "sso_personal_tenant_provisioned",
            provider=provider,
            organization_id=organization_id,
        )
        return organization_id

    async def _diagnose_collision(
        self, *, provider: str, provider_org_id: str, slug: str
    ) -> PersonalTenantCollision:
        """Name the key that actually collided.

        Read outside any tenant scope, on untenanted tables plus ``enterprises``
        (which has no ``organization_id`` and is therefore not enrolled in
        migration 018) — so the diagnosis itself cannot be hidden by RLS. The
        ``organizations`` probe is last and deliberately best-effort: it IS
        tenanted, so an invisible row simply does not answer, and an
        indeterminate diagnosis is reported as such rather than guessed.
        """
        async with get_db_session() as session:
            mapping = await session.get(SSOOrgMappingModel, (provider, provider_org_id))
            if mapping is not None:
                return PersonalTenantCollision(
                    "sso_org_mappings.provider_org_id", provider_org_id
                )
            enterprise = (
                await session.execute(
                    select(EnterpriseModel.enterprise_id).where(
                        EnterpriseModel.slug == slug
                    )
                )
            ).scalar_one_or_none()
            if enterprise is not None:
                return PersonalTenantCollision("enterprises.slug", slug)
            organization = (
                await session.execute(
                    select(OrganizationModel.organization_id).where(
                        OrganizationModel.slug == slug
                    )
                )
            ).scalar_one_or_none()
            if organization is not None:
                return PersonalTenantCollision("organizations.slug", slug)
        return PersonalTenantCollision("unknown", slug)

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
        """Enterprise, organization, team, mapping, subject row — one transaction."""
        async with get_db_session() as session:
            # The four tenant rows, in the operator path's order, from the one
            # writer both paths share. Its conflict refusals are unconditional;
            # what this module adds is the interpretation, above, using the
            # untenanted subject row the writer cannot see.
            tenant = await bootstrap_tenant(
                session,
                name=name,
                slug=slug,
                provider_org_id=provider_org_id,
                organization_id=organization_id,
            )

            # The subject binding — the row that answers "where does this
            # individual live?" on every later login, including the ones the IdP
            # reports no organization for. Written last so it is the final
            # arbiter of a race, and so nothing above it is visible without it.
            #
            # ``membership_confirmed`` starts False: the IdP membership is
            # established only once this transaction has committed, because a
            # membership is what makes the IdP echo the organization and an
            # echoed organization with no committed mapping is a permanent
            # ``sso_org_unmapped``.
            now = datetime.now(UTC)
            session.add(
                SSOPersonalOrgModel(
                    provider=provider,
                    provider_user_id=provider_user_id,
                    organization_id=tenant.organization.organization_id,
                    provider_org_id=provider_org_id,
                    enterprise_id=tenant.enterprise.enterprise_id,
                    membership_confirmed=False,
                    created_at=now,
                    updated_at=now,
                )
            )
