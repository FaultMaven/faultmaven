"""Persistence adapter for personal enterprises (#1045, ADR-016 D5, ADR-017 D3).

Implements ``ISSOPersonalEnterpriseRepository`` over
``sso_personal_enterprises``. Every table this path touches —
``sso_personal_enterprises``, ``sso_org_mappings`` and ``enterprises`` itself —
is outside RLS: the first two are read on the unauthenticated SSO callback
before a tenant is bound, and the third *is* the tenant.

**A sign-up creates an enterprise and nothing else** (ADR-017 D3/D5/D4). No
organization: that is a billing target created by payment, and a sign-in has no
way to know who pays. No team: that is formed by consent, and inventing one
would give an account a sharing group it never agreed to. Three rows are
written here — the enterprise, the IdP-organization mapping, and the subject
binding — and the first two come from ``tenant_bootstrap``'s per-row writers, so
what "already exists" means cannot drift between this path and the operator's.

Why this path needs no RLS-exempt role and no tenant binding
------------------------------------------------------------
``fm-provision-sso-org`` demands the owner DSN because it resolves an
organization by ``(enterprise_id, slug)`` — an id-blind lookup the
``organizations`` policy cannot satisfy. This path has no such lookup and, since
the organization and the team went away with ADR-017, no RLS-enrolled table left
to write. It therefore neither binds nor restores a tenant scope; the login
service binds the enterprise it resolved, afterwards and once.

Idempotency, races, and collisions that are not races
------------------------------------------------------
The whole write is one transaction, so it commits entirely or not at all. Two
concurrent first logins for the same subject cannot both succeed: they derive
the same slug and the same IdP organization, so the loser trips one of the
constraints and rolls back whole, leaving no rows behind. It then re-reads the
subject row and adopts the winner's enterprise.

A constraint violation is **not** automatically a lost race, and conflating the
two produced a permanent lockout with a log that named the wrong thing (#1045
review, item 4). So when the subject row does not explain the violation, this
module asks the database *which key collided* and says so. The enterprise arm of
that class is gone by construction: the shared writer adopts an existing
enterprise with the same slug rather than always inserting, which is safe
precisely because the slug is derived from the subject and nobody else can
produce it.

Retirement, and why the row is re-pointed rather than re-inserted
-----------------------------------------------------------------
``users.enterprise_id`` is NOT NULL (ADR-017 D3: every account is anchored to
exactly one enterprise), so the old "clear the anchor and let the next login
provision" mechanism has no state left to express. What replaces it is a
positive one: a retirement stamps ``retired_at`` and ``retirement_state`` on
this row and leaves it in place, and it is that policy — read through
``account_anchor`` — which decides whether the subject's next org-less sign-in
may provision again. :meth:`get` therefore reads **live rows only**, so a
retired subject falls through to the anchor check instead of resolving back
into the tenant it was taken out of; and :meth:`provision` **re-points the
retired row** instead of inserting beside it, because ``subject`` is the primary
key and there can only ever be one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Optional

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    SSOOrgMappingModel,
    SSOPersonalEnterpriseModel,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import (
    OrgAlreadyClaimed,
    RemapRefused,
    ensure_mapping,
    get_or_create_enterprise,
)
from faultmaven.modules.auth.contracts import (
    ISSOPersonalEnterpriseRepository,
    PersonalEnterpriseRecord,
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


class SessionlessSSOPersonalEnterpriseRepository(ISSOPersonalEnterpriseRepository):
    """One database session per operation, as the sibling mapping repository."""

    async def get(
        self, provider: str, provider_user_id: str
    ) -> Optional[PersonalEnterpriseRecord]:
        async with get_db_session() as session:
            # The subject is the whole primary key: an account has at most one
            # personal enterprise, whichever IdP minted it. The provider is
            # still compared, because a row minted by another provider is not
            # this provider's binding even though it names the same subject.
            #
            # ``retired_at`` is part of the predicate, not a caller's
            # afterthought: a retired row is kept precisely so the anchor check
            # can read the operator's next-login policy off it, and answering
            # with it here would resolve the subject straight back into the
            # tenant the retirement fenced them out of.
            row = await session.get(SSOPersonalEnterpriseModel, provider_user_id)
            if row is None or row.provider != provider or row.retired_at is not None:
                return None
            return PersonalEnterpriseRecord(
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
                SSOPersonalEnterpriseModel.provider == provider,
                SSOPersonalEnterpriseModel.created_at >= since,
            )
            return int((await session.execute(stmt)).scalar_one())

    async def confirm_membership(self, provider: str, provider_user_id: str) -> None:
        async with get_db_session() as session:
            await session.execute(
                update(SSOPersonalEnterpriseModel)
                .where(
                    SSOPersonalEnterpriseModel.provider == provider,
                    SSOPersonalEnterpriseModel.subject == provider_user_id,
                )
                .values(membership_confirmed=True, updated_at=datetime.now(UTC))
            )

    async def retire(self, provider: str, provider_user_id: str) -> bool:
        """Drop the binding. The enterprise and its cases are left in place.

        Used by the personal→company switch (#1320), where the account has just
        moved its anchor onto a company enterprise: the binding is deleted
        outright rather than stamped, because there is no next-login policy to
        record — the account no longer lives in a personal tenant at all, and a
        stamped row would tell the anchor check the opposite.
        """
        async with get_db_session() as session:
            row = await session.get(SSOPersonalEnterpriseModel, provider_user_id)
            if row is None or row.provider != provider:
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
        """Create the subject's enterprise atomically, or adopt an existing one."""
        try:
            # The id the WRITE actually used, not the one this call proposed.
            # ``get_or_create_enterprise`` adopts a live row with the same
            # derived slug rather than always inserting, and that row is what
            # the binding then names — so returning the proposed uuid would hand
            # the login an enterprise id that names nothing. Reachable through
            # the #1320 switch, which drops the binding and leaves the
            # enterprise and its mapping standing: the subject's next
            # provisioning conflicts with nothing, adopts, and would otherwise
            # bind an id ``get_enterprise`` cannot resolve.
            enterprise_id = await self._write(
                provider=provider,
                provider_user_id=provider_user_id,
                provider_org_id=provider_org_id,
                enterprise_id=str(uuid.uuid4()),
                name=name,
                slug=slug,
            )
        except _CONFLICT_SIGNALS:
            # The whole transaction rolled back, so nothing of ours survives.
            # Either a concurrent login for the same subject won — in which case
            # its enterprise is the one this login wants — or something else
            # owns a key we derived, which is a different problem with a
            # different remedy and must not be logged as a race.
            adopted = await self.get(provider, provider_user_id)
            if adopted is not None:
                logger.info(
                    "sso_personal_tenant_race_adopted",
                    provider=provider,
                    enterprise_id=adopted.enterprise_id,
                )
                return adopted.enterprise_id
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

        logger.info(
            "sso_personal_tenant_provisioned",
            provider=provider,
            enterprise_id=enterprise_id,
        )
        return enterprise_id

    async def _diagnose_collision(
        self, *, provider: str, provider_org_id: str, slug: str
    ) -> PersonalTenantCollision:
        """Name the key that actually collided.

        Read outside any tenant scope, on untenanted tables plus ``enterprises``
        (which is the tenant itself and is therefore not RLS-enrolled) — so the
        diagnosis itself cannot be hidden by RLS, and every key this path can
        collide on is reachable from here. The organizations probe that used to
        close the list is gone with the organization: a sign-up no longer writes
        one, so it is no longer a key this attempt could have collided on.
        """
        async with get_db_session() as session:
            mapping = await session.get(SSOOrgMappingModel, (provider, provider_org_id))
            if mapping is not None:
                return PersonalTenantCollision(
                    "sso_org_mappings.provider_org_id", provider_org_id
                )
            # LIVE rows only, matching the partial uniqueness rule. A retired
            # tenant keeps its slug, so naming it as the
            # collision would point an operator at a row that is not in
            # anybody's way — the "log names the wrong thing" failure again.
            enterprise = (
                await session.execute(
                    select(EnterpriseModel.enterprise_id).where(
                        EnterpriseModel.slug == slug,
                        EnterpriseModel.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if enterprise is not None:
                return PersonalTenantCollision("enterprises.slug", slug)
        return PersonalTenantCollision("unknown", slug)

    async def _write(
        self,
        *,
        provider: str,
        provider_user_id: str,
        provider_org_id: str,
        enterprise_id: str,
        name: str,
        slug: str,
    ) -> str:
        """Enterprise, mapping, subject row — one transaction, three rows.

        Returns the enterprise id the write **used**, which is not necessarily
        the one proposed: the slug arm adopts an existing live row.

        Three, not five: ADR-017 D5/D4 say a sign-up creates no organization and
        no team, so those rows are not written here and their absence is the
        design rather than an omission.
        """
        async with get_db_session() as session:
            # The enterprise, from the writer both provisioning paths share. Its
            # slug arm adopts an existing row rather than always inserting,
            # which is safe precisely because the slug is derived from the
            # subject: nobody else can produce it, so an orphan left by an
            # earlier failed attempt is this subject's own.
            enterprise, _ = await get_or_create_enterprise(
                session, enterprise_id=enterprise_id, name=name, slug=slug
            )
            await ensure_mapping(
                session,
                provider_org_id=provider_org_id,
                enterprise_id=enterprise.enterprise_id,
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
            #
            # An existing row is **re-pointed** rather than inserted beside:
            # ``subject`` is the primary key, so a subject whose earlier tenant
            # was retired with ``fresh-tenant`` has one row and this is it. The
            # retirement columns are cleared as part of the move — the policy
            # has now been honoured, and leaving it set would tell the next
            # anchor read that the tenant this call just created is retired.
            now = datetime.now(UTC)
            row = await session.get(SSOPersonalEnterpriseModel, provider_user_id)
            if row is None:
                session.add(
                    SSOPersonalEnterpriseModel(
                        subject=provider_user_id,
                        provider=provider,
                        provider_org_id=provider_org_id,
                        enterprise_id=enterprise.enterprise_id,
                        membership_confirmed=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.provider = provider
                row.provider_org_id = provider_org_id
                row.enterprise_id = enterprise.enterprise_id
                row.membership_confirmed = False
                # ``created_at`` is when THIS tenant was minted, and the
                # provisioning ceiling counts on it — a re-pointed row that kept
                # an old timestamp would be a fresh tenant the rate limit could
                # not see.
                row.created_at = now
                row.updated_at = now
                row.retired_at = None
                row.retirement_state = None
            return enterprise.enterprise_id
