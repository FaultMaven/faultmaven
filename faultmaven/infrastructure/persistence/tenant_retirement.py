"""The one writer that takes a personal tenant out of service (#1045 D8).

The counterpart of :mod:`faultmaven.infrastructure.persistence.tenant_bootstrap`.
That module writes the rows a tenant is made of; this one retires them, in an
order chosen so that a run interrupted anywhere leaves a state the operator can
finish from.

Nothing here deletes a case, an evidence artifact or a knowledge item, and
nothing renames anything. A retired tenant keeps its rows and its slug — the
uniqueness indexes on ``enterprises.slug`` and ``organizations (enterprise_id,
slug)`` are **partial on ``deleted_at IS NULL``** (migration 052), so the next
tenant for the same subject can derive exactly the same slug and still insert.
That is what removed the rename the previous design needed, and with it the
``LIKE``-and-``.first()`` lookup that could not tell one retired tenant of a
subject from another.

Addressing
----------
A **live** tenant is addressed by its subject, through the ``sso_personal_orgs``
binding row. A **retired or partly-retired** tenant is addressed by
**organization id** — the binding is one of the first things retirement removes,
and reconstructing it from a derived slug is exactly the ambiguity above. The
command prints the organization id, so an interrupted run can be finished.

The order, and why each step is where it is
-------------------------------------------
1. **Soft-delete the organization.** The fence. From here no login can enter the
   tenant: both resolution branches end in the bind-and-verify tail, which
   refuses an organization carrying ``deleted_at``.
2. **Revoke the subject's outstanding tokens.** The callback is not the only
   way in: a live refresh chain keeps minting for a tenant whose organization
   row is gone. The watermark closes it (and the refresh paths re-check the
   organization row — see ``organization_liveness``).
3. **Delete the ``sso_personal_orgs`` binding.** Before the provider calls,
   because while it exists with ``membership_confirmed`` false a login will ask
   the provider to *finish* the membership — re-creating, by its deterministic
   external id, the organization step 4 is about to remove.
4. **Delete the IdP membership and organization**, addressed by the
   ``provider_org_id`` recorded in **this tenant's mapping row** — never by a
   subject-derived external id, which a *later* tenant of the same subject would
   also answer to.
5. **Delete the mapping row.** After step 4, so the recorded id is still there
   to read; and once the IdP organization is gone the derived external id is
   free, so a fresh tenant provisioned afterwards cannot collide at the provider.
6. **Soft-delete the enterprise and record the policy.**
7. **``fresh-tenant`` only: clear the account's anchor.** Last, because it is the
   step that lets the subject provision again, and it must not take effect while
   any earlier step is outstanding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select

from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    SSOPersonalOrgModel,
)


class EnterpriseRowMissing(Exception):
    """The organization exists but the enterprise it names does not.

    A data fault, and reported as its own outcome rather than folded into "no
    such tenant": the remedies differ completely, and collapsing them told an
    operator their subject was absent when the truth was a broken row.
    """

    def __init__(self, organization_id: str, enterprise_id: str) -> None:
        super().__init__(
            f"organization {organization_id} names enterprise {enterprise_id}, "
            "which does not exist"
        )
        self.organization_id = organization_id
        self.enterprise_id = enterprise_id


@dataclass(frozen=True)
class SubjectBinding:
    """The ``sso_personal_orgs`` row, as an operator command reads it."""

    provider: str
    provider_user_id: str
    organization_id: str
    enterprise_id: str
    provider_org_id: str
    membership_confirmed: bool


@dataclass
class PersonalTenantState:
    """Everything a retirement has to act on, read in one pass."""

    organization_id: str
    enterprise_id: str
    organization_slug: str
    enterprise_slug: str
    organization_retired: bool
    enterprise_retired: bool
    retirement_policy: Optional[str]
    mapping_provider_org_id: Optional[str]
    binding: Optional[SubjectBinding]


def _binding_of(row) -> Optional[SubjectBinding]:
    if row is None:
        return None
    return SubjectBinding(
        provider=row.provider,
        provider_user_id=row.provider_user_id,
        organization_id=row.organization_id,
        enterprise_id=row.enterprise_id,
        provider_org_id=row.provider_org_id,
        membership_confirmed=bool(row.membership_confirmed),
    )


async def find_live_binding(
    session, *, provider: str, provider_user_id: str
) -> Optional[SubjectBinding]:
    """The subject's binding row, or None. The only subject-keyed lookup."""
    row = await session.get(SSOPersonalOrgModel, (provider, provider_user_id))
    return _binding_of(row)


async def _mapping_for(session, *, provider: str, organization_id: str):
    return (
        (
            await session.execute(
                select(SSOOrgMappingModel).where(
                    SSOOrgMappingModel.provider == provider,
                    SSOOrgMappingModel.organization_id == organization_id,
                )
            )
        )
        .scalars()
        .first()
    )


async def _binding_for(session, *, organization_id: str):
    return (
        (
            await session.execute(
                select(SSOPersonalOrgModel).where(
                    SSOPersonalOrgModel.organization_id == organization_id
                )
            )
        )
        .scalars()
        .first()
    )


async def read_state(
    session, *, provider: str, organization_id: str
) -> Optional[PersonalTenantState]:
    """Read the tenant addressed by ``organization_id``, or None if absent.

    Raises :class:`EnterpriseRowMissing` when the organization is there and its
    enterprise is not — a different fact from "no such tenant", with a different
    remedy.
    """
    organization = await session.get(OrganizationModel, organization_id)
    if organization is None:
        return None

    enterprise = await session.get(EnterpriseModel, organization.enterprise_id)
    if enterprise is None:
        raise EnterpriseRowMissing(organization_id, organization.enterprise_id)

    mapping = await _mapping_for(
        session, provider=provider, organization_id=organization_id
    )
    return PersonalTenantState(
        organization_id=organization.organization_id,
        enterprise_id=enterprise.enterprise_id,
        organization_slug=organization.slug,
        enterprise_slug=enterprise.slug,
        # Both halves, matching what ``soft_delete_organization`` writes: a row
        # carrying ``deleted_at`` but still ``is_active`` is half-fenced, and
        # reading that as retired would skip the step that finishes it.
        organization_retired=(
            organization.deleted_at is not None and not organization.is_active
        ),
        enterprise_retired=enterprise.deleted_at is not None,
        retirement_policy=enterprise.personal_tenant_retirement,
        mapping_provider_org_id=(
            mapping.provider_org_id if mapping is not None else None
        ),
        binding=_binding_of(
            await _binding_for(session, organization_id=organization_id)
        ),
    )


async def soft_delete_organization(session, *, organization_id: str) -> bool:
    """Step 1 — the fence. True when this call changed the row."""
    organization = await session.get(OrganizationModel, organization_id)
    if organization is None:
        return False
    if organization.deleted_at is not None and not organization.is_active:
        return False
    now = datetime.now(UTC)
    if organization.deleted_at is None:
        organization.deleted_at = now
    organization.is_active = False
    organization.updated_at = now
    await session.flush()
    return True


async def delete_binding(session, *, organization_id: str) -> Optional[SubjectBinding]:
    """Step 3 — drop the subject binding. Returns the row that was removed.

    Keyed on the **organization**, not the subject: ``UNIQUE (provider,
    organization_id)`` means at most one subject can name it, so this reaches the
    right row however the tenant was addressed, and a mistyped subject cannot
    delete somebody else's binding.
    """
    row = await _binding_for(session, organization_id=organization_id)
    if row is None:
        return None
    binding = _binding_of(row)
    await session.delete(row)
    await session.flush()
    return binding


async def delete_mapping(
    session, *, provider: str, organization_id: str
) -> Optional[str]:
    """Step 5 — drop the IdP-organization binding. Returns the id it named."""
    mapping = await _mapping_for(
        session, provider=provider, organization_id=organization_id
    )
    if mapping is None:
        return None
    provider_org_id = mapping.provider_org_id
    await session.delete(mapping)
    await session.flush()
    return provider_org_id


async def retire_enterprise(session, *, enterprise_id: str, policy: str) -> bool:
    """Step 6 — soft-delete the enterprise and record the operator's policy.

    One transaction, and one pair of typed columns: ``deleted_at`` says the
    enterprise is retired, ``personal_tenant_retirement`` says it was retired as
    somebody's **personal** tenant and which choice the operator made. Together
    they are the whole retirement state — there is no marker to parse, and
    nothing an unrelated settings write can clobber.

    ``deleted_at`` is set once and never re-stamped: a re-run must not move the
    retirement's timestamp, which is the bug a freshly-built marker had.
    """
    enterprise = await session.get(EnterpriseModel, enterprise_id)
    if enterprise is None:
        return False
    if enterprise.deleted_at is not None and enterprise.personal_tenant_retirement == (
        policy
    ):
        return False
    now = datetime.now(UTC)
    if enterprise.deleted_at is None:
        enterprise.deleted_at = now
    enterprise.personal_tenant_retirement = policy
    enterprise.updated_at = now
    await session.flush()
    return True
