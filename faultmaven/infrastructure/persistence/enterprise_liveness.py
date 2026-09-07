"""Is this enterprise usable as a tenant? One predicate, several callers.

``deleted_at IS NULL`` was written out at the SSO callback's bind-and-verify
tail, and would have been written out again at the operator command's tenant
load and a third time at the refresh guard. Three copies of a liveness rule is
how one of them ends up checking a different thing — which matters here, because
the retirement this rule has to notice is expressed by exactly that column.

**Why the enterprise and not the organization** (ADR-017 D1/D2): the tenant a
session lives inside is the enterprise. An organization is a cost centre; its
removal sets ``organization_id`` to NULL on the rows it paid for (the FK is
``ON DELETE SET NULL``) and takes nothing away from the account's access. So
"may this session continue?" is an enterprise question, and asking it of the
organization would end sessions for a billing change and miss retirement of the
tenant itself.
"""

from __future__ import annotations

import logging
from typing import Any, Optional


def enterprise_is_usable(enterprise: Optional[Any]) -> bool:
    """Whether a login may bind, or a token chain continue into, this tenant.

    Absence answers False: a caller that could not load the row does not know
    the tenant is usable, and this gate fails closed.
    """
    if enterprise is None:
        return False
    return getattr(enterprise, "deleted_at", None) is None


async def enterprise_id_is_usable(enterprise_id: Optional[str]) -> bool:
    """The same question, for callers that hold an id rather than a row.

    **Scope, stated precisely.** This answers "was this tenant taken out of
    service?", and only that. Two cases are deliberately NOT refused:

    * **no claim at all** — a separate condition with its own handling
      (``resolve_enterprise_claim`` and ``bind_request_enterprise_context``);
      answering False here would refuse every single-tenant refresh;
    * **no such row** — absence is not evidence of retirement, and it is the
      ordinary shape under single-tenant, where the claim is the Standalone
      sentinel. Retirement never deletes the enterprise row — it soft-deletes
      it — so the case this guard exists for always has a row to read.

    A lookup that fails answers True and logs: the request path still binds the
    tenant and fails closed, and RLS still scopes every read, so a database blip
    must not turn into a fleet-wide refresh outage.
    """
    if not enterprise_id:
        return True

    # Read the ROW, not a repository's view of it: a repository read filters
    # ``deleted_at IS NULL``, so a soft-deleted tenant comes back as None and
    # would be indistinguishable from an id that names nothing. The two answers
    # here are opposite, so the read has to be able to tell them apart.
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import EnterpriseModel

    try:
        async with get_db_session() as session:
            enterprise = await session.get(EnterpriseModel, enterprise_id)
    except Exception:  # noqa: BLE001 - see the docstring
        logging.getLogger(__name__).warning(
            "Could not read enterprise %s to check it is still in service; "
            "allowing the mint. The request path still binds and fails closed.",
            enterprise_id,
        )
        return True
    if enterprise is None:
        return True
    return enterprise_is_usable(enterprise)
