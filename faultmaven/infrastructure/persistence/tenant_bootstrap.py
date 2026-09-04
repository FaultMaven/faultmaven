"""The one writer that brings a tenant into existence (ADR-013, #869, #1045).

A tenant is four rows in a fixed order — enterprise, organization, default team,
and the ``sso_org_mappings`` row that binds an IdP organization to it — and
until now two call sites wrote them: ``fm-provision-sso-org`` for the operator
path and the personal-tenant repository for the login path. The CLI's own
docstring already conceded they write the same rows. Two copies of an ordering
constraint is one copy too many, so this module is the single writer and both
call sites pass their differences in as arguments.

**What the two callers genuinely differ on, and why it is a parameter here
rather than a fork of the code:**

* *What an existing row MEANS.* Both callers get the same refusals —
  ``RemapRefused`` and ``OrgAlreadyClaimed`` are raised unconditionally, because
  a writer that "does not refuse" ends up silently skipping the write and
  committing a half-tenant. What differs is the *interpretation*: for the
  operator a conflict is a catastrophe a human resolves, while the login path
  holds an untenanted subject row that tells it whether the conflict is its own
  concurrent attempt (adopt) or somebody else's key (refuse loudly). So the
  caller interprets; it does not get a knob that changes what is written.
* *How the organization is identified.* The operator resolves it by
  ``(enterprise_id, slug)`` — an id-blind lookup, which is exactly why that path
  needs an RLS-exempt role. The login path supplies the id it generated and
  bound. Both arrive here as an already-decided ``organization_id``.

**RLS.** Every write below except the enterprise targets an RLS-tenanted table
(migration 018), whose policy is created with no ``FOR`` clause — so ``USING``
doubles as ``WITH CHECK`` and an INSERT carrying a different ``organization_id``
than the session's ``app.current_org_id`` is *rejected*. This module does not
bind anything: the session it is handed already belongs to a transaction, and
the engine's ``begin`` listener sampled the contextvar when that transaction
opened. Binding is the caller's job, before it opens the session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from faultmaven.config.constants import STANDALONE_TEAM_NAME
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    TeamModel,
)

#: The only SSO provider FaultMaven ships an adapter for (ADR-015).
PROVIDER = "workos"


class RemapRefused(Exception):
    """The IdP org is already mapped to a different FaultMaven organization."""

    def __init__(self, provider_org_id: str, mapped_to: str, requested: str) -> None:
        super().__init__(provider_org_id)
        self.provider_org_id = provider_org_id
        self.mapped_to = mapped_to
        self.requested = requested


class OrgAlreadyClaimed(Exception):
    """The FaultMaven organization is already claimed by another IdP org."""

    def __init__(
        self, organization_id: str, claimed_by: str, requested_by: str
    ) -> None:
        super().__init__(organization_id)
        self.organization_id = organization_id
        self.claimed_by = claimed_by
        self.requested_by = requested_by


@dataclass(frozen=True)
class BootstrappedTenant:
    """What one bootstrap call created or found."""

    enterprise: EnterpriseModel
    enterprise_created: bool
    organization: OrganizationModel
    organization_created: bool
    team: TeamModel
    team_created: bool
    mapping_created: bool


async def get_or_create_enterprise(
    session, *, enterprise_id: str | None, name: str, slug: str
) -> tuple[EnterpriseModel, bool]:
    """Return (enterprise, created). Looks up by id, then by slug.

    The slug arm is not only an operator convenience. ``organizations`` has no
    ``ON DELETE CASCADE`` to ``enterprises``, so hard-deleting a personal
    organization leaves its enterprise behind; a login that re-derived the same
    slug and always INSERTed would then collide forever on
    ``enterprises.slug``'s unique index and be refused as somebody else's
    tenant (#1045 review, item 4a). Adopting the orphan is safe precisely
    because the slug is derived from the subject: nobody else can produce it.
    """
    if enterprise_id:
        found = await session.get(EnterpriseModel, enterprise_id)
        if found is None:
            raise LookupError(f"No enterprise with enterprise_id={enterprise_id}")
        return found, False

    # LIVE rows only. Since migration 052 the slug uniqueness rules are partial
    # on ``deleted_at IS NULL`` — a retired tenant keeps its slug — so a writer
    # that adopted a soft-deleted row would hand a "fresh" tenant straight back
    # to the retired one it is supposed to replace. The lookup has to be scoped
    # exactly the way the constraint is, or the two disagree about what "already
    # exists" means.
    existing = (
        await session.execute(
            select(EnterpriseModel).where(
                EnterpriseModel.slug == slug,
                EnterpriseModel.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = datetime.now(UTC)
    enterprise = EnterpriseModel(
        enterprise_id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        created_at=now,
        updated_at=now,
    )
    session.add(enterprise)
    await session.flush()
    return enterprise, True


async def get_or_create_organization(
    session, *, enterprise_id: str, name: str, slug: str, organization_id: str | None
) -> tuple[OrganizationModel, bool]:
    """Return (organization, created). Identity is (enterprise_id, slug).

    ``organization_id`` is the id to use when one has to be created. The login
    path supplies the value it already bound as the tenant context; the operator
    path passes ``None`` and gets a fresh uuid.

    Resolving by slug is what makes a re-run a no-op — and it is also why the
    operator path needs an RLS-exempt role: ``organizations`` is RLS-tenanted
    and its policy keys on ``organization_id``, so a scoped role could not read
    this row without already knowing the id the lookup exists to find. The login
    path escapes that because it never has to look one up without an id: its
    untenanted subject row answered that question before this call.
    """
    # LIVE rows only, for the reason ``get_or_create_enterprise`` states.
    existing = (
        await session.execute(
            select(OrganizationModel).where(
                OrganizationModel.enterprise_id == enterprise_id,
                OrganizationModel.slug == slug,
                OrganizationModel.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = datetime.now(UTC)
    organization = OrganizationModel(
        organization_id=organization_id or str(uuid.uuid4()),
        enterprise_id=enterprise_id,
        name=name,
        slug=slug,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(organization)
    await session.flush()
    return organization, True


async def get_or_create_default_team(
    session, *, organization_id: str
) -> tuple[TeamModel, bool]:
    """Return (team, created). One default team per organization (ADR-013)."""
    existing = (
        await session.execute(
            select(TeamModel).where(
                TeamModel.organization_id == organization_id,
                TeamModel.name == STANDALONE_TEAM_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = datetime.now(UTC)
    team = TeamModel(
        team_id=str(uuid.uuid4()),
        organization_id=organization_id,
        name=STANDALONE_TEAM_NAME,
        description="Default team for this organization",
        created_at=now,
        updated_at=now,
    )
    session.add(team)
    await session.flush()
    return team, True


async def find_mapping(session, *, provider_org_id: str):
    """Return the mapping row for this IdP org, or None."""
    return await session.get(SSOOrgMappingModel, (PROVIDER, provider_org_id))


async def ensure_mapping(
    session, *, provider_org_id: str, organization_id: str
) -> bool:
    """Create the mapping row if absent. Returns True when created.

    Both directions of the 1:1 relation are ways to bind the wrong customers
    together, so both are refused — for **every** caller, with no policy knob:

    * ``RemapRefused`` — this IdP org already points at a *different*
      organization. Repointing changes which tenant existing users land in.
    * ``OrgAlreadyClaimed`` — this organization is already claimed by a
      *different* IdP org. This is what the ``UNIQUE (provider,
      organization_id)`` constraint would otherwise raise, and it is the alarm
      that fires when a slug collision has silently resolved a new customer onto
      someone else's tenant.

    An earlier version of this let the login path pass ``refuse_conflicts=False``
    so it could adopt a racer's tenant. That was wrong in a way worth recording:
    "do not refuse" became "return False without writing", so a login whose IdP
    organization was already mapped elsewhere committed a tenant **with no
    mapping row** — a silent half-tenant, which is precisely the state the whole
    design refuses to leave behind.

    The refusals are therefore unconditional, and the *interpretation* is the
    caller's: only the login path holds the untenanted subject row that
    distinguishes "my own concurrent attempt won" (adopt it) from "somebody else
    owns this key" (refuse loudly). A writer cannot make that call, so it does
    not try — it reports, and the caller decides.
    """
    existing = await find_mapping(session, provider_org_id=provider_org_id)
    if existing is not None:
        if existing.organization_id != organization_id:
            raise RemapRefused(
                provider_org_id, existing.organization_id, organization_id
            )
        return False

    claimed = (
        await session.execute(
            select(SSOOrgMappingModel).where(
                SSOOrgMappingModel.provider == PROVIDER,
                SSOOrgMappingModel.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if claimed is not None:
        raise OrgAlreadyClaimed(
            organization_id, claimed.provider_org_id, provider_org_id
        )

    now = datetime.now(UTC)
    session.add(
        SSOOrgMappingModel(
            provider=PROVIDER,
            provider_org_id=provider_org_id,
            organization_id=organization_id,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return True


async def bootstrap_tenant(
    session,
    *,
    name: str,
    slug: str,
    provider_org_id: str,
    enterprise_id: str | None = None,
    organization_id: str | None = None,
) -> BootstrappedTenant:
    """Write enterprise → organization → default team → mapping, in that order.

    Does **not** commit: the caller owns the transaction, which is what lets the
    login path add its own subject row to the same one and get an all-or-nothing
    tenant.
    """
    enterprise, enterprise_created = await get_or_create_enterprise(
        session, enterprise_id=enterprise_id, name=name, slug=slug
    )
    organization, organization_created = await get_or_create_organization(
        session,
        enterprise_id=enterprise.enterprise_id,
        name=name,
        slug=slug,
        organization_id=organization_id,
    )
    team, team_created = await get_or_create_default_team(
        session, organization_id=organization.organization_id
    )
    mapping_created = await ensure_mapping(
        session,
        provider_org_id=provider_org_id,
        organization_id=organization.organization_id,
    )
    return BootstrappedTenant(
        enterprise=enterprise,
        enterprise_created=enterprise_created,
        organization=organization,
        organization_created=organization_created,
        team=team,
        team_created=team_created,
        mapping_created=mapping_created,
    )
