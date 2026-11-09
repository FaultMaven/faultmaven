"""The LLM usage view: ``GET /api/v1/admin/llm/usage`` (#640, #1764).

Reads the usage ledger (``infrastructure/llm/usage_ledger.py``) back for the
operator: the enterprise the request is bound to, and nothing else. The query
names that enterprise, and under ``TENANT_PROVIDER=multi`` RLS backs it.

**Not audited.** It is the operator's own tenant, read as numbers and ids with
no case content, with the standing of ``GET /admin/llm/config``. A cross-tenant
rollup would be audited; there is none (#640 Q2).

**Never zeros for an unreadable store.** A database error answers 503
``DATABASE_UNAVAILABLE`` with ``Retry-After: 5``: a zero-filled 200 would read
as "no spend".
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from faultmaven.api.dependencies import llm_usage_report
from faultmaven.api.exception_handlers import DATABASE_UNAVAILABLE
from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.models import LLMUsageReport
from faultmaven.config.settings import get_settings
from faultmaven.config.tenant_context import (
    UNSCOPED_REQUEST_MSG,
    get_current_enterprise_id,
    usable_tenant_id,
)
from faultmaven.exceptions import UsageStoreUnavailable
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.providers.tenancy.factory import (
    BUILTIN_MULTI,
    requested_tenant_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin - Usage"])

#: #640 Q3 (a): per-user figures are exposed to the operator in standalone only.
USER_GROUPING_REFUSED_DETAIL = (
    "group_by=user is not available under multi-tenancy: per-user figures are "
    "shown to the operator only on a single-tenant deployment."
)

USAGE_UNAVAILABLE_DETAIL = (
    "The LLM usage store could not be read, so no usage is reported. "
    "Please retry shortly."
)


@router.get(
    "/llm/usage",
    response_model=LLMUsageReport,
    responses={
        401: {"description": "Not authenticated"},
        403: {
            "description": (
                "Not a platform administrator, or the request is not scoped "
                "to an enterprise"
            )
        },
        422: {
            "description": (
                "An unknown `window` or `group_by`, or `group_by=user` under "
                "multi-tenancy"
            )
        },
        503: {
            "description": (
                "`DATABASE_UNAVAILABLE`: the usage store could not be read. "
                "Never answered as a zero-filled 200."
            ),
            "headers": {
                "x-error-code": {
                    "description": "DATABASE_UNAVAILABLE",
                    "schema": {"type": "string"},
                },
                "Retry-After": {
                    "description": "Seconds to wait before retrying",
                    "schema": {"type": "string"},
                },
            },
        },
    },
)
async def get_llm_usage(
    window: Literal["1d", "7d", "30d", "90d"] = Query(
        "7d", description="Whole UTC days ending today, inclusive"
    ),
    group_by: Literal["provider_model", "user", "billing_subject"] = Query(
        "provider_model", description="What `groups` is keyed by"
    ),
    current_user: AuthenticatedUser = Depends(require_platform_admin),
) -> LLMUsageReport:
    """The bound enterprise's LLM usage, read from the usage ledger.

    Costs are estimated at call time from the price table, never billed.
    Coverage starts when the ledger was deployed; there is no backfill.
    """
    multi = requested_tenant_provider() == BUILTIN_MULTI
    if multi and group_by == "user":
        raise HTTPException(status_code=422, detail=USER_GROUPING_REFUSED_DETAIL)
    enterprise_id = usable_tenant_id(get_current_enterprise_id())
    if enterprise_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=UNSCOPED_REQUEST_MSG
        )

    settings = get_settings()
    try:
        report = await llm_usage_report(
            enterprise_id=enterprise_id,
            window=window,
            group_by=group_by,
            soft_budget=settings.prompt_budget.turn_token_budget,
            ceiling=settings.prompt_budget.turn_token_ceiling,
            turn_retention_days=settings.observability.llm_usage_turn_retention_days,
            # #640 Q3: an account subject's id is a user id, shown to the
            # operator in standalone only.
            hide_account_ids=multi,
        )
    except UsageStoreUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=USAGE_UNAVAILABLE_DETAIL,
            headers={"x-error-code": DATABASE_UNAVAILABLE, "Retry-After": "5"},
        )
    report["scope"] = {
        "enterprise_id": enterprise_id,
        "deployment": "cloud" if settings.is_cloud else "standalone",
        "tenancy": "multi" if multi else "single",
    }
    return LLMUsageReport.model_validate(report)
