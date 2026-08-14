"""Audit trail for the operator-role CLIs (fm#1050).

``fm-promote-platform-admin`` granted the highest privilege the deployment
offers and recorded nothing: after the fm#819 cutover ``user_audit_log`` held
exactly one row — SSO JIT provisioning — and none for the promotion. Demotion
had the same gap. This module is the one writer that closes it.

**Called from the grant's own single writer, not from the CLI.**
``bootstrap.data_init.assign_operator_roles`` performs every grant — the
hand-run promotion *and* the re-grant that runs on every startup — so it is
where the record is written. Auditing in the promote command instead left the
startup path silent: a demotion followed by a restart produced a trail showing
a revocation and no re-grant, while the account held ``platform_admin`` again.
Demotion has no such shared writer and calls this directly.

**Which table, and why not the obvious one.** ``user_audit_log`` is RLS-tenanted
(migration 018) and ``platform_admin`` is deployment-scoped (ADR-012 D9), so
there is no organization to stamp such a row with. Under ``TENANT_PROVIDER=multi``
the standalone default organization does not exist, so the write would fail its
FK — on precisely the deployment where the gap was found — and naming a real
tenant instead would bury a deployment-wide privilege change inside one
customer's trail. ``operator_access_audit`` is untenanted, append-only by
trigger, and keeps identifiers unreferenced so evidence outlives the account.

**Who the row is "about".** ``operator_user_id``/``operator_username`` carry the
account whose operator status changed, not the human who ran the command. There
is no authenticated actor on a ``kubectl exec`` path to record, and the useful
question a reviewer asks of this table — "show me everything about operator X" —
is answered by making the subject the key: their promotion, their accesses and
their demotion come back from one ``list_access(operator_user_id=X)``. That the
actor is unattributable is stated in ``details`` rather than left to inference.

**Failure posture: report loudly, do not undo.** The repository lets write
failures propagate, and for a *read* the caller is expected to fail the request
— an unaudited read must not be served. A role change is different: the grant is
already persisted by the time this runs, so raising cannot un-grant it, and
refusing at startup would leave a standalone deployment with no operator, which
is unusable (see ``assign_operator_roles``). So the CLIs report the failure and
exit non-zero, leaving an operator who can see that the trail is incomplete.

⚠️ A retry does NOT repair a failed audit: the grant is idempotent, so the second
run finds nothing missing, grants nothing, and therefore writes no row. Treat a
non-zero exit here as "verify the trail by hand", not as "run it again".
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from faultmaven.models.interfaces_operator_audit import OperatorAction

logger = logging.getLogger(__name__)


def _deployment_mode() -> Optional[str]:
    """The deployment mode as a plain string, or None if settings are unusable.

    ``settings.deployment_mode`` is a plain ``str`` on some paths and a
    ``DeploymentMode`` member on others, and a bare ``str()`` on the enum yields
    ``"DeploymentMode.CLOUD"`` — a value no ``deployment_mode = 'cloud'`` query
    would ever match, stored permanently in an append-only row.
    """
    try:
        from faultmaven.config.settings import get_settings

        mode = get_settings().deployment_mode
        return str(getattr(mode, "value", mode))
    except Exception:  # pragma: no cover - settings unavailable
        return None


async def record_operator_role_change(
    *,
    action: OperatorAction,
    user: Any,
    roles_changed: Sequence[str],
    invoked_via: str,
) -> None:
    """Append one operator-role event. Raises if it cannot be persisted.

    Args:
        action: ``ROLE_GRANTED`` or ``ROLE_REVOKED``.
        user: The account whose operator status changed.
        roles_changed: The roles this operation actually added or removed.
        invoked_via: The console entrypoint, recorded so the trail distinguishes
            a hand-run promotion from the startup re-grant.
    """
    from faultmaven.infrastructure.persistence.sessionless_operator_audit_repository import (
        SessionlessOperatorAuditRepository,
    )

    await SessionlessOperatorAuditRepository().record_access(
        operator_user_id=getattr(user, "user_id", None),
        operator_username=getattr(user, "username", None),
        action=action,
        # No organization: the role is deployment-scoped. NULL here already
        # means "spanned all tenants" for the access actions, which is the same
        # thing a deployment-scoped privilege means.
        target_organization_id=None,
        deployment_mode=_deployment_mode(),
        details={
            "roles_changed": list(roles_changed),
            "resulting_roles": list(getattr(user, "roles", None) or []),
            "invoked_via": invoked_via,
            # Stated, not implied: nothing authenticates the human who ran this.
            "actor": "unauthenticated_cli",
        },
    )
