"""The one writer that takes a personal tenant out of service (#1045 D8).

The counterpart of :mod:`faultmaven.infrastructure.persistence.tenant_bootstrap`.
That module writes the rows a tenant is made of; this one retires them, in an
order chosen so that a run interrupted anywhere leaves a state the **same**
command completes from.

Nothing here deletes a case, an evidence artifact or a knowledge item. A retired
tenant's data stays exactly where it is — retention is ADR-014's subject, not
this command's — so every step below is a soft delete, a rename, or the removal
of a *binding* row.

The order, and why each step is where it is
-------------------------------------------
1. **Soft-delete the organization.** The fence. From here no login can enter the
   tenant: both resolution branches end in the login service's bind-and-verify
   tail, which refuses an organization carrying ``deleted_at``. Doing anything
   else first would leave a window in which a login still lands in a tenant that
   is being taken apart.
2. **Delete the ``sso_org_mappings`` row.** An IdP organization that still
   echoes now meets an unmapped branch (an operator-fixable refusal) instead of
   binding a half-retired tenant.
3. **Delete the ``sso_personal_orgs`` row.** Before the IdP calls, deliberately:
   while that row exists with ``membership_confirmed`` false, a login would call
   the provider to *finish* the membership — re-creating, by its deterministic
   external id, the very organization step 4 is about to remove.
4. **Delete the IdP membership, then the IdP organization.** Frees the derived
   ``external_id`` so a later tenant for the same subject can claim it.
5. **Rename the organization's slug.** Frees the derived slug on the
   organization side.
6. **Retire the enterprise — soft-delete, rename, and record the marker — in one
   transaction.** Last, because the marker is what a later login reads to decide
   whether the subject may provision again. Written any earlier it would release
   an anchor while the derived slug was still occupied, and the "fresh" tenant
   the login then tried to provision would collide with the tenant being retired.

Every step is idempotent and every step is discoverable from the arguments
alone: the organization and the enterprise by the slug derived from the subject
(in either its live or its retired form), the IdP organization by the external
id derived from the same subject. So no step depends on a row an earlier step
removed, which is what makes the sequence resumable rather than merely ordered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy import select

from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    OrganizationModel,
    SSOOrgMappingModel,
    SSOPersonalOrgModel,
)
from faultmaven.utils.serialization import decode_json_blob


@dataclass(frozen=True)
class SubjectBinding:
    """The ``sso_personal_orgs`` row, as an operator command needs to read it."""

    provider: str
    provider_user_id: str
    organization_id: str
    enterprise_id: str
    provider_org_id: str
    membership_confirmed: bool


@dataclass
class PersonalTenantState:
    """Everything a retirement has to act on, read in one pass.

    Mutable and re-read between steps rather than carried across them: each step
    opens its own transaction, so a snapshot taken before the first would be a
    claim about the past by the time the last one ran.
    """

    organization_id: str
    enterprise_id: str
    organization_slug: str
    enterprise_slug: str
    organization_retired: bool
    enterprise_retired: bool
    mapping_provider_org_id: Optional[str]
    binding: Optional[SubjectBinding]
    retirement_marker: Optional[dict]


def _settings_dict(value: Any) -> dict:
    """``enterprises.settings`` as a dict, whatever the backend handed back."""
    return decode_json_blob(value, copy=True) or {}


def read_retirement_marker(settings_value: Any, marker_key: str) -> Optional[dict]:
    """The retirement marker inside a ``settings`` value, or None.

    Answers None — never raises — for an enterprise with no settings, settings
    that are not a JSON object, or a marker that is not one either. An ordinary
    company enterprise reaching this read is the common case, not an error, and
    a reader that raised there would turn every mapped login with a stale anchor
    into a 500.
    """
    marker = _settings_dict(settings_value).get(marker_key)
    return marker if isinstance(marker, dict) else None


async def _organization_by_id(session, organization_id: str):
    return await session.get(OrganizationModel, organization_id)


async def _organization_by_slug(session, *, slug: str, pattern: str):
    """The organization carrying ``slug``, or the one already retired from it.

    Two lookups rather than one so a resumed run finds the tenant whether or not
    the rename step (5) had already landed. The pattern arm is ordered second:
    the live slug is the identity a running tenant has, and a retired row must
    never shadow it.
    """
    live = (
        (
            await session.execute(
                select(OrganizationModel).where(OrganizationModel.slug == slug)
            )
        )
        .scalars()
        .first()
    )
    if live is not None:
        return live
    return (
        (
            await session.execute(
                select(OrganizationModel).where(OrganizationModel.slug.like(pattern))
            )
        )
        .scalars()
        .first()
    )


async def _binding_for(session, *, organization_id: str):
    row = (
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


async def read_state(
    session,
    *,
    provider: str,
    marker_key: str,
    organization_id: Optional[str] = None,
    slug: Optional[str] = None,
    retired_slug_pattern: Optional[str] = None,
) -> Optional[PersonalTenantState]:
    """Locate the tenant and read everything a retirement step needs.

    Addressed either by ``organization_id`` (the operator names it) or by the
    ``slug``/``retired_slug_pattern`` pair derived from the subject. The derived
    form is what makes a retirement resumable: it is computed from the command's
    own arguments, so it keeps working after the subject row that recorded the
    organization id has been deleted.

    Returns None when no such tenant exists — which is a real answer ("nothing
    to do"), not a failure.
    """
    organization = None
    if organization_id:
        organization = await _organization_by_id(session, organization_id)
    elif slug:
        organization = await _organization_by_slug(
            session, slug=slug, pattern=retired_slug_pattern or f"{slug}%"
        )
    if organization is None:
        return None

    enterprise = await session.get(EnterpriseModel, organization.enterprise_id)
    if enterprise is None:
        # An organization whose enterprise is gone cannot be retired coherently:
        # the marker has nowhere to live and the account's anchor points at a
        # row that does not exist. Report it as absent rather than half-retire.
        return None

    mapping = await _mapping_for(
        session, provider=provider, organization_id=organization.organization_id
    )
    return PersonalTenantState(
        organization_id=organization.organization_id,
        enterprise_id=enterprise.enterprise_id,
        organization_slug=organization.slug,
        enterprise_slug=enterprise.slug,
        # Both halves, matching what ``soft_delete_organization`` writes: an
        # organization carrying ``deleted_at`` but still ``is_active`` is
        # half-fenced, and reading that as retired would skip the step that
        # finishes it.
        organization_retired=(
            organization.deleted_at is not None and not organization.is_active
        ),
        enterprise_retired=enterprise.deleted_at is not None,
        mapping_provider_org_id=(
            mapping.provider_org_id if mapping is not None else None
        ),
        binding=await _binding_for(
            session, organization_id=organization.organization_id
        ),
        retirement_marker=read_retirement_marker(enterprise.settings, marker_key),
    )


async def soft_delete_organization(session, *, organization_id: str) -> bool:
    """Step 1 — the fence. True when this call changed the row.

    ``deleted_at`` **and** ``is_active`` together, because the login service's
    bind-and-verify tail refuses on either and an operator reading the row
    should not have to know which one the code happens to check.
    """
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


async def delete_mapping(
    session, *, provider: str, organization_id: str
) -> Optional[str]:
    """Step 2 — drop the IdP-organization binding. Returns the id it named."""
    mapping = await _mapping_for(
        session, provider=provider, organization_id=organization_id
    )
    if mapping is None:
        return None
    provider_org_id = mapping.provider_org_id
    await session.delete(mapping)
    await session.flush()
    return provider_org_id


async def delete_binding(session, *, organization_id: str) -> Optional[SubjectBinding]:
    """Step 3 — drop the subject binding. Returns the row that was removed.

    Keyed on the **organization**, not the subject: ``UNIQUE (provider,
    organization_id)`` means at most one subject can name it, so this reaches
    the right row whether the operator addressed the tenant by subject or by
    organization id — and it cannot delete some other subject's binding by
    naming a subject that was mistyped.
    """
    row = (
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
    if row is None:
        return None
    binding = SubjectBinding(
        provider=row.provider,
        provider_user_id=row.provider_user_id,
        organization_id=row.organization_id,
        enterprise_id=row.enterprise_id,
        provider_org_id=row.provider_org_id,
        membership_confirmed=bool(row.membership_confirmed),
    )
    await session.delete(row)
    await session.flush()
    return binding


async def rename_organization(session, *, organization_id: str, slug: str) -> bool:
    """Step 5 — free the derived slug on the organization side."""
    organization = await session.get(OrganizationModel, organization_id)
    if organization is None or organization.slug == slug:
        return False
    organization.slug = slug
    organization.updated_at = datetime.now(UTC)
    await session.flush()
    return True


async def retire_enterprise(
    session,
    *,
    enterprise_id: str,
    slug: str,
    marker_key: str,
    marker: dict,
) -> bool:
    """Step 6 — soft-delete, rename and mark the enterprise, atomically.

    One transaction on purpose. The marker is what a later login reads to decide
    whether this subject may provision again; releasing that anchor while the
    derived slug was still occupied would send the login into a collision with
    the tenant being retired. Making the two writes inseparable is what rules
    that out, rather than an ordering comment nobody can enforce.

    Existing settings are merged, never replaced: an enterprise's settings hold
    SSO and plan configuration that has nothing to do with this.
    """
    enterprise = await session.get(EnterpriseModel, enterprise_id)
    if enterprise is None:
        return False
    settings = _settings_dict(enterprise.settings)
    if (
        enterprise.deleted_at is not None
        and enterprise.slug == slug
        and settings.get(marker_key) == marker
    ):
        return False
    now = datetime.now(UTC)
    settings[marker_key] = marker
    enterprise.settings = json.dumps(settings)
    enterprise.slug = slug
    if enterprise.deleted_at is None:
        enterprise.deleted_at = now
    enterprise.updated_at = now
    await session.flush()
    return True


def build_marker(
    *,
    provider: str,
    key: str,
    policy: str,
    organization_id: str,
    retired_at: Optional[datetime] = None,
) -> dict:
    """The marker payload, in the one place both writer and reader agree on.

    ``key`` is the derived personal-tenant key, and it is what binds the marker
    to a single subject: a login honours a marker only when it re-derives the
    same value from its own identity, so a marker cannot release an anchor for
    anybody but the person it was written for.
    """
    return {
        "provider": provider,
        "key": key,
        "policy": policy,
        "organization_id": organization_id,
        "retired_at": (retired_at or datetime.now(UTC)).isoformat(),
    }
