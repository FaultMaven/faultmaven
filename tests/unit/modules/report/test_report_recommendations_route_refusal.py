"""The reports-module recommendations route refuses instead of fabricating.

``GET /api/v1/reports/recommendations/{case_id}`` is mounted, and its
``rec_service`` is unconditionally None (the container registers None), so its
only executable branch used to return ``action="generate"``, "Recommendation
service unavailable - all report types available" — a recommendation from a
similarity search that never ran, the #944 fail-open shape, live (fm#1030
review, RIDER 3). The runbook recommendation IS a claim about what the
knowledge base holds; a route that cannot search must refuse, matching the
case-module route's refusal contract (503, static detail).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.modules.report.api.routes import get_report_recommendations

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_an_unwired_recommendation_service_is_a_503_not_a_generate_verdict():
    case_service = MagicMock()
    case_service.get_case = AsyncMock(return_value=MagicMock(organization_id="o1"))
    current_user = MagicMock()
    current_user.user_id = "u1"

    with pytest.raises(HTTPException) as excinfo:
        await get_report_recommendations(
            case_id="case-1",
            current_user=current_user,
            tenant_provider=None,
            case_service=case_service,
            rec_service=None,
        )

    assert excinfo.value.status_code == 503
    detail = str(excinfo.value.detail)
    # A refusal, not a recommendation: the fabricated "all report types
    # available" verdict is gone, the cause is named, and the caller is
    # pointed at the surface that actually works.
    assert "not wired" in detail
    assert "report-recommendations" in detail
    assert "all report types available" not in detail
