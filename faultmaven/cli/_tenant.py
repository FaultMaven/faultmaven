"""The one way an operator command binds and resolves an enterprise.

Four commands take an ``--enterprise-id`` and every one of them has to do the
same two things before it reads anything: **bind** the enterprise, because RLS
scopes every table it is about to touch by ``app.current_enterprise_id`` and the
engine samples the contextvar when a transaction opens (#935) — so a session
opened first runs unbound — and then **check** that the id names a tenant this
deployment will act on.

They were each doing a different subset. ``fm-reassign-cases`` bound and checked
the row resolved; ``fm-set-turn-cap`` bound and checked nothing; ``fm-remove-org-member``
bound and checked the *organization*, never the enterprise; ``fm-personal-tenant``
refused the sentinel and applied ``enterprise_is_usable``. Four commands, four
answers to one question — and the two that checked least are the two that write
destructively.

One function now, and it is the union of what any of them needed:

* the **Standalone sentinel is refused** through ``usable_tenant_id``, the same
  predicate the request front door and the route dependency use. Under ``multi``
  the sentinel identifies the deployment rather than a tenant (fm#850); under
  ``single`` it is the deployment's one legitimate tenant and passes;
* the **row must be usable** by ``enterprise_is_usable`` — the same predicate the
  login's bind-and-verify tail applies, so a command cannot accept a tenant a
  login would refuse. A soft-deleted enterprise does not resolve.

The bind happens before the load, deliberately: the load is itself a read, and it
should run under the scope everything after it runs under.
"""

from __future__ import annotations

from typing import Any, Optional


class EnterpriseRefused(Exception):
    """The named enterprise is not one this command will act on.

    Carries the operator-facing sentence. Every caller prints it and stops
    without writing, which is the whole contract: a command that has refused
    its tenant has not read anything it could act on.
    """


async def bind_and_load_enterprise(
    enterprise_id: str, *, repository: Optional[Any] = None
) -> Any:
    """Bind ``enterprise_id`` for RLS and return its row, or raise.

    Args:
        enterprise_id: The id the operator named. An id, never a slug.
        repository: An ``IEnterpriseRepository`` to load through. Defaults to the
            sessionless one, which is what every command uses; injectable so a
            test can drive the refusals without a database.

    Returns:
        The ``Enterprise`` row, already bound as the current tenant.

    Raises:
        EnterpriseRefused: the id is the Standalone sentinel under multi-tenant,
            or names no usable enterprise. Nothing has been written either way.
    """
    from faultmaven.config.tenant_context import (
        set_current_enterprise_id,
        usable_tenant_id,
    )
    from faultmaven.infrastructure.persistence.enterprise_liveness import (
        enterprise_is_usable,
    )

    if not usable_tenant_id(enterprise_id):
        raise EnterpriseRefused(
            f"'{enterprise_id}' is not a tenant this deployment will act on: it "
            "is the Standalone sentinel, which identifies the deployment itself "
            "(fm#850). Nothing was written."
        )

    set_current_enterprise_id(enterprise_id)

    if repository is None:
        from faultmaven.infrastructure.persistence.sessionless_enterprise_repository import (  # noqa: E501
            SessionlessEnterpriseRepository,
        )

        repository = SessionlessEnterpriseRepository()

    enterprise = await repository.get_enterprise(enterprise_id)
    if not enterprise_is_usable(enterprise):
        raise EnterpriseRefused(
            f"No usable enterprise '{enterprise_id}' is visible.\n"
            "   Check the id (it is an id, not a slug), and note that a "
            "soft-deleted enterprise does not resolve. Nothing was written."
        )
    return enterprise
