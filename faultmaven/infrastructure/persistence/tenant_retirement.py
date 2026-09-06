"""The one writer that takes a personal tenant out of service (#1045 D8).

The counterpart of :mod:`faultmaven.infrastructure.persistence.tenant_bootstrap`.
That module writes the rows a tenant is made of; this one retires them, in an
order chosen so that a run interrupted anywhere leaves a state the operator can
finish from.

Nothing here deletes a case, an evidence artifact or a knowledge item, and
nothing renames anything. A retired tenant keeps its rows and its slug — the
uniqueness index on ``enterprises.slug`` is **partial on ``deleted_at IS NULL``**,
so the next tenant for the same subject can derive exactly the same slug and
still insert. That is what removed the rename the previous design needed, and
with it the ``LIKE``-and-``.first()`` lookup that could not tell one retired
tenant of a subject from another.

**The tenant is the ENTERPRISE** (ADR-017 D1). This module used to fence the
personal *organization* and retire the enterprise behind it; under ADR-017 the
organization neither isolates nor is created at sign-up, so the enterprise is
both the fence and the thing retired — one row instead of two, and one fewer
partial state an interrupted run can stop in.

Addressing
----------
A **live** tenant is addressed by its subject, through the
``sso_personal_enterprises`` binding row. A **retired or partly-retired** tenant
is addressed by **enterprise id** — the binding is one of the first things
retirement removes, and reconstructing it from a derived slug is exactly the
ambiguity above. The command prints the enterprise id, so an interrupted run can
be finished.

The order, and why each step is where it is
-------------------------------------------
1. **Soft-delete the enterprise.** The fence. From here no login can enter the
   tenant: every resolution branch ends in the bind-and-verify tail, which
   refuses an enterprise carrying ``deleted_at``.
2. **Revoke the subject's outstanding tokens.** The callback is not the only
   way in: a live refresh chain keeps minting for a tenant whose enterprise row
   is fenced. The watermark closes it (and the refresh paths re-check the
   enterprise row — see ``enterprise_liveness``).
3. **Record the retirement on the binding**, and leave the row in place. The
   state lives on ``sso_personal_enterprises`` (``retired_at``,
   ``retirement_state``) rather than on ``enterprises``, because the sign-in
   that must tell "this subject's own tenant was retired" from "the company
   that owned this account is gone" reads the subject, not the enterprise.
   Recorded before the provider calls, because while a **live** binding exists
   with ``membership_confirmed`` false a login will ask the provider to *finish*
   the membership — re-creating, by its deterministic external id, the IdP
   organization step 4 is about to remove. Stamping ``retired_at`` is what makes
   the binding stop being live: the repository's ``get`` reads live rows only.
4. **Delete the IdP membership and organization**, addressed by the
   ``provider_org_id`` recorded in **this tenant's mapping row** — never by a
   subject-derived external id, which a *later* tenant of the same subject would
   also answer to.
5. **Delete the mapping row.** After step 4, so the recorded id is still there
   to read; and once the IdP organization is gone the derived external id is
   free, so a fresh tenant provisioned afterwards cannot collide at the provider.

There is no sixth step, and its absence is the ADR-017 change. The old one
cleared ``users.enterprise_id`` so an org-less login would provision again;
that column is now NOT NULL (D3: every account is anchored to exactly one
enterprise), so the release could no longer be an absence. It is step 3's
``retirement_state`` instead — a positive record of what the operator chose,
which ``account_anchor.releases_provisioning`` reads. The account stays anchored
to the enterprise it was in, which is now fenced, and that is the whole of what
"retired" means for it.

The binding row therefore survives a retirement rather than being deleted. It has
to: ``subject`` is its primary key, so the subject has exactly one, and it is the
only place the operator's next-login decision can live. A later fresh
provisioning **re-points** it (see the repository), which is also what clears the
retirement it has by then honoured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select

from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    SSOOrgMappingModel,
    SSOPersonalEnterpriseModel,
)


class EnterpriseRowMissing(Exception):
    """The binding names an enterprise that does not exist.

    A data fault, and reported as its own outcome rather than folded into "no
    such tenant": the remedies differ completely, and collapsing them told an
    operator their subject was absent when the truth was a broken row.
    """

    def __init__(self, subject: str, enterprise_id: str) -> None:
        super().__init__(
            f"subject {subject} names enterprise {enterprise_id}, "
            "which does not exist"
        )
        self.subject = subject
        self.enterprise_id = enterprise_id


@dataclass(frozen=True)
class SubjectBinding:
    """The ``sso_personal_enterprises`` row, as an operator command reads it."""

    provider: str
    provider_user_id: str
    enterprise_id: str
    provider_org_id: str
    membership_confirmed: bool


@dataclass
class PersonalTenantState:
    """Everything a retirement has to act on, read in one pass."""

    enterprise_id: str
    enterprise_slug: str
    enterprise_retired: bool
    retirement_policy: Optional[str]
    mapping_provider_org_id: Optional[str]
    binding: Optional[SubjectBinding]


def _binding_of(row) -> Optional[SubjectBinding]:
    if row is None:
        return None
    return SubjectBinding(
        provider=row.provider,
        provider_user_id=row.subject,
        enterprise_id=row.enterprise_id,
        provider_org_id=row.provider_org_id,
        membership_confirmed=bool(row.membership_confirmed),
    )


async def find_live_binding(
    session, *, provider: str, provider_user_id: str
) -> Optional[SubjectBinding]:
    """The subject's LIVE binding row, or None. The only subject-keyed lookup.

    ``retired_at IS NULL`` is part of the predicate rather than a caller's
    afterthought: a retired binding is kept precisely so a later sign-in can
    read the retirement, and treating it as live would let a re-run retire an
    already-retired tenant a second time.
    """
    row = await session.get(SSOPersonalEnterpriseModel, provider_user_id)
    if row is None or row.provider != provider or row.retired_at is not None:
        return None
    return _binding_of(row)


async def _mapping_for(session, *, provider: str, enterprise_id: str):
    return (
        (
            await session.execute(
                select(SSOOrgMappingModel).where(
                    SSOOrgMappingModel.provider == provider,
                    SSOOrgMappingModel.enterprise_id == enterprise_id,
                )
            )
        )
        .scalars()
        .first()
    )


async def _binding_for(session, *, enterprise_id: str):
    return (
        (
            await session.execute(
                select(SSOPersonalEnterpriseModel).where(
                    SSOPersonalEnterpriseModel.enterprise_id == enterprise_id
                )
            )
        )
        .scalars()
        .first()
    )


async def read_state(
    session, *, provider: str, enterprise_id: str
) -> Optional[PersonalTenantState]:
    """Read the tenant addressed by ``enterprise_id``, or None if absent."""
    enterprise = await session.get(EnterpriseModel, enterprise_id)
    if enterprise is None:
        return None

    mapping = await _mapping_for(
        session, provider=provider, enterprise_id=enterprise_id
    )
    row = await _binding_for(session, enterprise_id=enterprise_id)
    return PersonalTenantState(
        enterprise_id=enterprise.enterprise_id,
        enterprise_slug=enterprise.slug,
        enterprise_retired=enterprise.deleted_at is not None,
        retirement_policy=row.retirement_state if row is not None else None,
        mapping_provider_org_id=(
            mapping.provider_org_id if mapping is not None else None
        ),
        binding=_binding_of(row),
    )


async def soft_delete_enterprise(session, *, enterprise_id: str) -> bool:
    """Step 1 — the fence. True when this call changed the row.

    ``deleted_at`` is set once and never re-stamped: a re-run must not move the
    retirement's timestamp, which is the bug a freshly-built marker had.
    """
    enterprise = await session.get(EnterpriseModel, enterprise_id)
    if enterprise is None:
        return False
    if enterprise.deleted_at is not None:
        return False
    now = datetime.now(UTC)
    enterprise.deleted_at = now
    enterprise.updated_at = now
    await session.flush()
    return True


async def record_retirement(session, *, enterprise_id: str, policy: str) -> bool:
    """Step 3 — stamp the retirement on the SUBJECT's binding row.

    Here rather than on ``enterprises`` because the sign-in that has to tell
    "this subject's own tenant was retired" from "the company that owned this
    account is gone" reads the subject. ``retired_at`` is set once and never
    re-stamped, for the reason ``soft_delete_enterprise`` states.
    """
    row = await _binding_for(session, enterprise_id=enterprise_id)
    if row is None:
        return False
    if row.retired_at is not None and row.retirement_state == policy:
        return False
    now = datetime.now(UTC)
    if row.retired_at is None:
        row.retired_at = now
    row.retirement_state = policy
    row.updated_at = now
    await session.flush()
    return True


async def delete_binding(session, *, enterprise_id: str) -> Optional[SubjectBinding]:
    """Drop the subject binding outright. Returns the row that was removed.

    **Not part of a retirement** — that stamps the row and keeps it, because the
    stamp is what a later sign-in reads. This is the re-anchor path (#1320's
    operator half): the account has moved onto a company enterprise, so there is
    no next-login policy to record and a stamped row would tell the anchor check
    the opposite of the truth.

    Keyed on the **enterprise**, not the subject: ``UNIQUE (enterprise_id)``
    means at most one subject can name it, so this reaches the right row however
    the tenant was addressed, and a mistyped subject cannot delete somebody
    else's binding.
    """
    row = await _binding_for(session, enterprise_id=enterprise_id)
    if row is None:
        return None
    binding = _binding_of(row)
    await session.delete(row)
    await session.flush()
    return binding


async def delete_mapping(
    session, *, provider: str, enterprise_id: str
) -> Optional[str]:
    """Step 5 — drop the IdP-organization binding. Returns the id it named."""
    mapping = await _mapping_for(
        session, provider=provider, enterprise_id=enterprise_id
    )
    if mapping is None:
        return None
    provider_org_id = mapping.provider_org_id
    await session.delete(mapping)
    await session.flush()
    return provider_org_id
