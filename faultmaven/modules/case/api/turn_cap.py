"""The route-level enforcement of the per-tenant turn cap (ADR-016 D5.3).

Why here, and why exactly here
------------------------------
``POST /cases/{case_id}/turns`` is the **only** operation in the API that
consumes an investigation turn. Every client reaches it through that one route —
the Copilot extension, the Dashboard, the Slack agent and any API caller alike —
and ``InvestigationService.process_turn`` has no second HTTP door.
``tests/integration/api/test_turn_cap_surface_inventory.py`` is what keeps that
true after this PR: it reads the live OpenAPI document *and* the live route
objects on every run, and fails when an operation either detector flags carries
no recorded verdict — or when this guard lands on an operation not recorded as
capped, which would spend a tenant's allowance on something that is not a turn.

Declared as a route ``dependencies=[...]`` entry rather than called from inside
the handler, so the guard is part of the route's *signature*: a refactor that
reorganises the 400-line handler body cannot drop it without deleting a line
that plainly says what it is, and the inventory test can see that it is
installed by inspecting the dependency tree rather than by parsing the body.

The trade-off that placement carries, stated rather than hidden: FastAPI runs
dependencies before the handler, so a turn that the handler would itself have
refused — no query and no attachment, an unknown case, a closed case — consumes
a unit of the day's allowance. The cost falls entirely on the tenant that sent
the malformed request, and the alternative (reserve late, deep in the body,
after every validation) buys a rare accounting nicety by putting the one guard
that bounds the bill somewhere a refactor can lose it.

What a refusal costs the caller elsewhere
-----------------------------------------
Nothing on the quota that protects LLM compute. The refusal is marked on
``request.state`` and ``RateLimitMiddleware`` releases this request's entry from
the per-session write windows on the way out (see
``api/middleware/rate_limiting``): a turn this cap refused ran no model, so
charging it to the bucket that exists to bound model spend would meter the same
event twice and let a capped tenant throttle its own reads. The ``global``
address-keyed window keeps its entry — that one bounds request volume, which a
capped caller still generates.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.tenant_context import UNSCOPED_REQUEST_MSG, get_current_tenant_id
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    TenantTurnCapExceeded,
    TenantTurnCapUnavailable,
    reserve_turn,
)

logger = logging.getLogger(__name__)

#: ``x-error-code`` on a cap refusal. Distinct from the rate limiter's 429 so a
#: client can tell "slow down" from "come back tomorrow" — they carry the same
#: status and want opposite reactions.
TURN_CAP_ERROR_CODE = "TENANT_TURN_CAP_EXCEEDED"

#: ``x-error-code`` when the cap itself could not be applied. A 503, not a 429:
#: the caller has done nothing wrong and the wait is not until midnight.
TURN_CAP_UNAVAILABLE_ERROR_CODE = "TENANT_TURN_CAP_UNAVAILABLE"

#: Attribute ``RateLimitMiddleware`` reads on the way out. Set only by refusals
#: that performed no model work.
RATE_LIMIT_REFUND_ATTR = "rate_limit_refund"


def _mark_for_refund(request: Request) -> None:
    """Tell the rate limiter this request cost no LLM compute.

    Best-effort by construction: the attribute is simply absent when the
    middleware is not in the stack, and the middleware treats a missing one as
    "nothing to release", so neither side has to know whether the other is
    present.
    """
    try:
        setattr(request.state, RATE_LIMIT_REFUND_ATTR, True)
    except Exception:  # pragma: no cover - Request.state is always writable
        logger.debug("turn cap: could not mark the request for a rate-limit refund")


async def enforce_tenant_turn_cap(
    request: Request,
    _authenticated=Depends(require_authentication),
) -> None:
    """Reserve one turn against the caller's tenant allowance, or refuse.

    ``_authenticated`` is declared and never read, and that is load-bearing:
    FastAPI inserts a route's ``dependencies=[...]`` entries at the FRONT of the
    dependant list, so without it this would run *before* the handler's own
    ``Depends(require_authentication)`` — an unauthenticated POST would be
    answered 403 by the tenant check instead of 401, and in single-tenant mode
    (where the binder forces the Standalone org regardless of who is asking) it
    would charge a ledger entry to a request that is about to be rejected.
    Naming the dependency here puts authentication ahead of it in the same tree.

    The organization comes from the **bound tenant context** rather than from
    the actor's claim, and those are not always the same value: single-tenant
    deployments force the Standalone org, ignoring any org an injected claim
    carries. The ledger row must satisfy the RLS ``WITH CHECK`` on
    ``app.current_org_id``, so the bound value is the only one that can be
    written — reading the claim instead would turn a mismatch into an opaque
    integrity error on a path that must not guess.

    Raises:
        HTTPException: 429 at the cap, 503 when the cap cannot be applied, 403
            when the request carries no usable tenant.
    """
    organization_id = get_current_tenant_id()
    if not organization_id:
        # Unreachable through the front door — ``bind_request_org_context``
        # already refuses an unscoped request — and kept because this dependency
        # must not be the place that decides an unscoped request is uncapped.
        _mark_for_refund(request)
        logger.warning("turn cap: refusing a turn that carries no usable tenant")
        raise HTTPException(status_code=403, detail=UNSCOPED_REQUEST_MSG)

    try:
        reservation = await reserve_turn(organization_id)
    except TenantTurnCapExceeded as exceeded:
        _mark_for_refund(request)
        # Invariant 6's observability half. INFO rather than WARNING: a tenant
        # reaching a cap that exists to be reached is the mechanism working, and
        # a WARNING here would train operators to ignore the channel. The
        # organization id and the count are both present because the operator's
        # next action — raise this tenant's cap, or leave it — needs both.
        logger.info(
            "turn cap: refused a turn for organization %s (%s/%s used today, "
            "policy=%s); resets at %s",
            exceeded.organization_id,
            exceeded.used,
            exceeded.limit,
            exceeded.source,
            exceeded.reset_at.isoformat(),
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exceeded.user_message,
            headers={
                "Retry-After": str(exceeded.retry_after_seconds),
                "x-error-code": TURN_CAP_ERROR_CODE,
            },
        ) from exceeded
    except TenantTurnCapUnavailable as unavailable:
        _mark_for_refund(request)
        logger.error(
            "turn cap: refusing a turn for organization %s because the cap "
            "could not be applied: %s",
            organization_id,
            unavailable,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Your usage allowance could not be checked just now. "
                "Please try again in a moment."
            ),
            headers={
                "Retry-After": "10",
                "x-error-code": TURN_CAP_UNAVAILABLE_ERROR_CODE,
            },
        ) from unavailable

    logger.debug(
        "turn cap: admitted turn %s for organization %s (limit=%s, policy=%s)",
        reservation.used,
        reservation.organization_id,
        reservation.limit,
        reservation.source,
    )
