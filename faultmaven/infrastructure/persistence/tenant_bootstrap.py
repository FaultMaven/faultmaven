"""The writers that bring a tenant into existence (ADR-013, ADR-017, #869, #1045).

Two call sites create tenants and they no longer create the same rows, so this
module holds the per-row writers both share plus the one composite the operator
path needs:

* **the operator path** (``fm-provision-sso-org``) onboards a paying customer:
  enterprise, organization, default team, and the ``sso_org_mappings`` row that
  binds an IdP organization to it. :func:`bootstrap_tenant` writes those four,
  in that order.
* **the sign-up path** (the SSO login) creates an **enterprise and nothing
  else** (ADR-017 D3/D5/D4). An organization is a billing target created by
  payment and a team is formed by consent, so a sign-in — which knows neither —
  must not invent them. It composes :func:`get_or_create_enterprise` (or
  :func:`get_or_create_enterprise_for_domain`) with :func:`ensure_mapping`
  itself.

They share the per-row writers rather than a single composite, because the rows
they write genuinely differ now; what has to stay shared is each row's own rule
(what an existing row means, which lookups are live-only), and that is what
these functions are.

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
* *How the tenant is identified.* The operator resolves the organization by
  ``(enterprise_id, slug)`` — an id-blind lookup, which is exactly why that path
  needs an RLS-exempt role. The sign-up path resolves the enterprise by the
  domain it derived, or generates a private one, and writes no organization at
  all.

**RLS.** ``organizations`` and ``teams`` are RLS-tenanted, and their policies are
created with no ``FOR`` clause — so ``USING`` doubles as ``WITH CHECK`` and an
INSERT carrying a different ``enterprise_id`` than the session's
``app.current_enterprise_id`` is *rejected*. ``enterprises`` and
``sso_org_mappings`` are not enrolled: the enterprise IS the tenant, and the
mapping is read on the unauthenticated callback before one is bound. This module
does not bind anything: the session it is handed already belongs to a
transaction, and the engine's ``begin`` listener sampled the contextvar when
that transaction opened. Binding is the caller's job, before it opens the
session — and the sign-up path, which now touches only unenrolled tables, has
nothing left to bind.

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
    """The IdP org is already mapped to a different FaultMaven enterprise."""

    def __init__(self, provider_org_id: str, mapped_to: str, requested: str) -> None:
        super().__init__(provider_org_id)
        self.provider_org_id = provider_org_id
        self.mapped_to = mapped_to
        self.requested = requested


class OrgAlreadyClaimed(Exception):
    """The FaultMaven enterprise is already claimed by another IdP org."""

    def __init__(self, enterprise_id: str, claimed_by: str, requested_by: str) -> None:
        super().__init__(enterprise_id)
        self.enterprise_id = enterprise_id
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

    The slug arm is not only an operator convenience. A personal enterprise can
    outlive the binding that named it — ``repository.retire()`` drops the
    binding on the #1320 personal→company switch and leaves the enterprise and
    its mapping standing — so a later login for that subject re-derives the same
    slug. A writer that always INSERTed would collide forever on
    ``enterprises.slug``'s unique index and be refused as somebody else's tenant
    (#1045 review, item 4a). Adopting is safe precisely because the slug is
    derived from the subject: nobody else can produce it.

    Callers must use the returned row's id rather than the one they proposed;
    on the adopt arm they differ, and binding the proposed one names no row.
    """
    if enterprise_id:
        found = await session.get(EnterpriseModel, enterprise_id)
        if found is not None:
            return found, False
        # NOT a LookupError any more. Under ADR-017 the login path generates the
        # ENTERPRISE id and binds it before opening the transaction (RLS refuses
        # a write for an unbound tenant), so it arrives here naming a row that
        # does not exist yet and that this call is what creates. Refusing would
        # make first sign-in impossible. The operator path passes an id only for
        # an enterprise it has already resolved, so it never reaches this arm.

    # LIVE rows only. The slug uniqueness rules are partial on
    # ``deleted_at IS NULL`` — a retired tenant keeps its slug — so a writer
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
        enterprise_id=enterprise_id or str(uuid.uuid4()),
        name=name,
        slug=slug,
        created_at=now,
        updated_at=now,
    )
    session.add(enterprise)
    await session.flush()
    return enterprise, True


async def get_or_create_enterprise_for_domain(
    session, *, domain: str, name: str, slug: str
) -> tuple[EnterpriseModel, bool]:
    """Return (enterprise, created) for an email domain (ADR-017 D3).

    The lookup key is ``enterprises.domain``, **not** the slug: the domain is
    the fact sign-up derived, and keying on it is what makes "the domain has
    exactly one enterprise" true. The slug is a derived identifier that happens
    to be a function of the same domain; looking up by it would work today and
    silently stop working the day the derivation changed.

    LIVE rows only, matching the partial uniqueness index exactly. A retired
    enterprise keeps its domain, so a writer that adopted a soft-deleted row
    would hand the next sign-up from that domain straight back into the tenant
    an operator took out of service — and the index would not stop it, because
    the index does not see retired rows either. The lookup has to be scoped the
    way the constraint is or the two disagree about what "already exists" means.

    Creating is **not** owning: the first account from a domain gains nothing by
    being first (D3). It does not administer the enterprise, and the enterprise
    has no administrator at all until a domain claim is verified (D7).
    """
    if not domain:
        raise ValueError("a domain enterprise needs a domain")
    folded = domain.casefold()
    existing = (
        await session.execute(
            select(EnterpriseModel).where(
                EnterpriseModel.domain == folded,
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
        domain=folded,
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
    session, *, enterprise_id: str
) -> tuple[TeamModel, bool]:
    """Return (team, created). One default team per ENTERPRISE (ADR-017 D4)."""
    existing = (
        await session.execute(
            select(TeamModel).where(
                TeamModel.enterprise_id == enterprise_id,
                TeamModel.name == STANDALONE_TEAM_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = datetime.now(UTC)
    team = TeamModel(
        team_id=str(uuid.uuid4()),
        enterprise_id=enterprise_id,
        name=STANDALONE_TEAM_NAME,
        description="Default team for this enterprise",
        created_at=now,
        updated_at=now,
    )
    session.add(team)
    await session.flush()
    return team, True


async def find_mapping(session, *, provider_org_id: str):
    """Return the mapping row for this IdP org, or None."""
    return await session.get(SSOOrgMappingModel, (PROVIDER, provider_org_id))


async def ensure_mapping(session, *, provider_org_id: str, enterprise_id: str) -> bool:
    """Create the mapping row if absent. Returns True when created.

    Both directions of the 1:1 relation are ways to bind the wrong customers
    together, so both are refused — for **every** caller, with no policy knob:

    * ``RemapRefused`` — this IdP org already points at a *different*
      enterprise. Repointing changes which tenant existing users land in.
    * ``OrgAlreadyClaimed`` — this enterprise is already claimed by a
      *different* IdP org. This is what the ``UNIQUE (provider, enterprise_id)``
      constraint would otherwise raise, and it is the alarm that fires when a
      slug collision has silently resolved a new customer onto someone else's
      tenant.

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
        if existing.enterprise_id != enterprise_id:
            raise RemapRefused(provider_org_id, existing.enterprise_id, enterprise_id)
        return False

    claimed = (
        await session.execute(
            select(SSOOrgMappingModel).where(
                SSOOrgMappingModel.provider == PROVIDER,
                SSOOrgMappingModel.enterprise_id == enterprise_id,
            )
        )
    ).scalar_one_or_none()
    if claimed is not None:
        raise OrgAlreadyClaimed(enterprise_id, claimed.provider_org_id, provider_org_id)

    now = datetime.now(UTC)
    session.add(
        SSOOrgMappingModel(
            provider=PROVIDER,
            provider_org_id=provider_org_id,
            enterprise_id=enterprise_id,
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
        session, enterprise_id=enterprise.enterprise_id
    )
    mapping_created = await ensure_mapping(
        session,
        provider_org_id=provider_org_id,
        enterprise_id=enterprise.enterprise_id,
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
