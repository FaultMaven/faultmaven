"""Is this organization usable as a tenant? One predicate, three callers.

``deleted_at IS NULL AND is_active`` was written out at the SSO callback's
bind-and-verify tail, and would have been written out again at the operator
command's company-org load and a third time at the refresh guard. Three copies
of a liveness rule is how one of them ends up checking a different thing —
which matters here, because the retirement this rule now has to notice is
expressed by exactly those two columns.
"""

from __future__ import annotations

import logging
from typing import Any, Optional


def organization_is_usable(organization: Optional[Any]) -> bool:
    """Whether a login may bind, or a token chain continue into, this tenant.

    Absence answers False: a caller that could not load the row does not know
    the tenant is usable, and this gate fails closed.
    """
    if organization is None:
        return False
    if getattr(organization, "deleted_at", None) is not None:
        return False
    return bool(getattr(organization, "is_active", True))


async def organization_id_is_usable(organization_id: Optional[str]) -> bool:
    """The same question, for callers that hold an id rather than a row.

    **Scope, stated precisely.** This answers "was this tenant taken out of
    service?", and only that. Two cases are deliberately NOT refused:

    * **no claim at all** — a separate condition with its own handling
      (``resolve_organization_claim`` and ``bind_request_org_context``);
      answering False here would refuse every single-tenant refresh;
    * **no such row** — absence is not evidence of retirement, and it is the
      ordinary shape under single-tenant, where the claim is the Standalone
      sentinel. Retirement never deletes the organization row — it soft-deletes
      it — so the case this guard exists for always has a row to read.

    A lookup that fails answers True and logs: the request path still binds the
    tenant and fails closed, and RLS still scopes every read, so a database blip
    must not turn into a fleet-wide refresh outage.
    """
    if not organization_id:
        return True

    # Read the ROW, not the repository's view of it: ``get_organization``
    # filters ``deleted_at IS NULL``, so a soft-deleted tenant comes back as
    # None and would be indistinguishable from an id that names nothing. The two
    # answers here are opposite, so the read has to be able to tell them apart.
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import OrganizationModel

    try:
        async with get_db_session() as session:
            organization = await session.get(OrganizationModel, organization_id)
    except Exception:  # noqa: BLE001 - see the docstring
        logging.getLogger(__name__).warning(
            "Could not read organization %s to check it is still in service; "
            "allowing the mint. The request path still binds and fails closed.",
            organization_id,
        )
        return True
    if organization is None:
        return True
    return organization_is_usable(organization)
